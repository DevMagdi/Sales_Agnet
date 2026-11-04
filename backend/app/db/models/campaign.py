"""Campaigns, prospects, templates, mailboxes, messages, replies, tasks and demos."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, OrgScopedMixin, TimestampMixin, UUIDPkMixin


class MailboxPool(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "mailbox_pools"

    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Mailbox(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "mailboxes"

    pool_id: Mapped[str | None] = mapped_column(String(36), index=True)
    from_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String(320))
    provider: Mapped[str] = mapped_column(String(20), default="smtp", nullable=False)
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_username: Mapped[str | None] = mapped_column(String(320))
    smtp_password_enc: Mapped[str | None] = mapped_column(Text)
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int | None] = mapped_column(Integer)
    imap_username: Mapped[str | None] = mapped_column(String(320))
    imap_password_enc: Mapped[str | None] = mapped_column(Text)
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    daily_cap: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    warmup_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warmup_stage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    signature_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    physical_address: Mapped[str] = mapped_column(Text, default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Campaign(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True, nullable=False)
    industry_filter: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    country_filter: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    city_filter: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    min_icp_score: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    language_policy: Mapped[str] = mapped_column(String(10), default="auto", nullable=False)
    template_set: Mapped[str] = mapped_column(String(60), default="default", nullable=False)
    mailbox_pool_id: Mapped[str | None] = mapped_column(String(36), index=True)
    daily_cap: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    ramp_schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    send_windows: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Cairo", nullable=False)
    review_first_n: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    auto_reply_classes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    channels_enabled: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["email"], nullable=False)
    max_followups: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    followup_gaps_days: Mapped[list[int]] = mapped_column(JSON, default=lambda: [3, 7], nullable=False)
    allow_risky_emails: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))
    stopped_reason: Mapped[str | None] = mapped_column(String(300))


class CampaignProspect(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "campaign_prospects"

    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id"), index=True, nullable=False)
    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    contact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(30), default="discovered", index=True, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_id: Mapped[str | None] = mapped_column(String(36))
    thread_id: Mapped[str | None] = mapped_column(String(120), index=True)
    outcome: Mapped[str | None] = mapped_column(String(40))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(80))
    last_step_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    re_queued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="ar", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "company_id", name="uq_campaign_prospects_campaign_company"),
        Index("ix_campaign_prospects_due", "state", "next_action_at"),
    )


class Template(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "templates"

    code: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    locale: Mapped[str] = mapped_column(String(8), default="ar", nullable=False)
    step: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    variables: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (UniqueConstraint("code", "locale", "version", name="uq_templates_code_locale_version"),)


class OutreachMessage(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "outreach_messages"

    campaign_prospect_id: Mapped[str | None] = mapped_column(String(36), index=True)
    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    contact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    direction: Mapped[str] = mapped_column(String(10), default="outbound", index=True, nullable=False)
    step: Mapped[str] = mapped_column(String(30), default="initial", nullable=False)
    locale: Mapped[str] = mapped_column(String(8), default="ar", nullable=False)
    subject: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(255))
    references: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(120), index=True)
    mailbox_id: Mapped[str | None] = mapped_column(String(36), index=True)
    template_code: Mapped[str | None] = mapped_column(String(80))
    template_version: Mapped[int | None] = mapped_column(Integer)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # exactly-once: a conditional UPDATE claims the row; the owner and expiry make the claim visible
    locked_by: Mapped[str | None] = mapped_column(String(80))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    llm_call_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), index=True)
    sender_email: Mapped[str | None] = mapped_column(String(320))
    to_email_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    to_domain: Mapped[str | None] = mapped_column(String(253), index=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claim_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outreach_messages_idempotency_key"),
        Index("ix_outreach_messages_status_scheduled", "status", "scheduled_for"),
    )


class MessageEvent(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "message_events"

    message_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InboundMessage(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "inbound_messages"

    channel: Mapped[str] = mapped_column(String(20), default="email", index=True, nullable=False)
    rfc_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    from_email_enc: Mapped[str | None] = mapped_column(Text)
    from_email_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    from_phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    from_label: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    subject: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cleaned_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), index=True)
    references: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    raw_headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)
    contact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    campaign_prospect_id: Mapped[str | None] = mapped_column(String(36), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Reply(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "replies"

    inbound_message_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    campaign_prospect_id: Mapped[str | None] = mapped_column(String(36), index=True)
    classification: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True, nullable=False)
    handled_by: Mapped[str | None] = mapped_column(String(36))
    opt_out_flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    llm_call_id: Mapped[str | None] = mapped_column(String(36))
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Task(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "tasks"

    type: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)
    contact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    reply_id: Mapped[str | None] = mapped_column(String(36), index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    brief: Mapped[str] = mapped_column(Text, default="", nullable=False)
    draft: Mapped[str] = mapped_column(Text, default="", nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open", index=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DemoRequest(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "demo_requests"

    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    contact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    source_reply_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="requested", index=True, nullable=False)
    proposed_slots: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_to: Mapped[str | None] = mapped_column(String(36))
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
