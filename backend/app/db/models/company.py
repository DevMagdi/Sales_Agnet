"""Companies, contacts, research evidence and industry playbooks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, OrgScopedMixin, TimestampMixin, UUIDPkMixin


class Company(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    name_ar: Mapped[str | None] = mapped_column(String(300))
    domain: Mapped[str | None] = mapped_column(String(253), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(String(500))
    country: Mapped[str] = mapped_column(String(2), default="EG", index=True, nullable=False)
    city: Mapped[str | None] = mapped_column(String(120), index=True)
    industrial_zone: Mapped[str | None] = mapped_column(String(120), index=True)
    industry: Mapped[str | None] = mapped_column(String(80), index=True)
    sub_industry: Mapped[str | None] = mapped_column(String(120))
    size_hint: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    employee_range: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), default="manual", index=True, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(500))
    place_id: Mapped[str | None] = mapped_column(String(200))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str] = mapped_column(String(10), default="unknown", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="discovered", index=True, nullable=False)
    icp_score: Mapped[int] = mapped_column(Integer, default=0, index=True, nullable=False)
    icp_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    excluded_reason: Mapped[str | None] = mapped_column(String(200))
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    manual_overrides: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (Index("ix_companies_status_score", "status", "icp_score"),)


class Contact(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "contacts"

    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role_title: Mapped[str | None] = mapped_column(String(200))
    role_category: Mapped[str] = mapped_column(String(20), default="unknown", index=True, nullable=False)
    email_enc: Mapped[str | None] = mapped_column(Text)
    email_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    email_is_role_based: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_status: Mapped[str] = mapped_column(String(20), default="unverified", index=True, nullable=False)
    email_source_url: Mapped[str | None] = mapped_column(String(500))
    phone_enc: Mapped[str | None] = mapped_column(Text)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    phone_type: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    phone_source_url: Mapped[str | None] = mapped_column(String(500))
    preferred_language: Mapped[str] = mapped_column(String(10), default="ar", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_source: Mapped[str | None] = mapped_column(String(40))

    __table_args__ = (
        UniqueConstraint("company_id", "email_hash", name="uq_contacts_company_email"),
        Index("ix_contacts_company_primary", "company_id", "is_primary"),
    )


class ResearchSnapshot(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "research_snapshots"

    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fetched_pages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class IndustryPlaybookRow(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "industry_playbooks"

    code: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(200), nullable=False)
    purchased_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    pain_hypotheses: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    demo_scenario: Mapped[str] = mapped_column(Text, default="", nullable=False)
    example_rfq_items: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    typical_zones: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    regulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PageCache(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    """Fetched page text (compressed) with content hash; re-crawled only when stale."""

    __tablename__ = "page_cache"

    company_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(1000), index=True, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    text_compressed: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    final_url: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (UniqueConstraint("company_id", "url", name="uq_page_cache_company_url"),)


class DiscoveryJob(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "discovery_jobs"

    kind: Mapped[str] = mapped_column(String(20), default="places", nullable=False)
    query: Mapped[str] = mapped_column(String(300), nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="EG", nullable=False)
    city: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(80))
    max_results: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    budget_cap_usd: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True, nullable=False)
    found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))


class ImportBatch(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "import_batches"

    filename: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="preview", nullable=False)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))


class SavedMapping(UUIDPkMixin, TimestampMixin, OrgScopedMixin, Base):
    __tablename__ = "saved_mappings"

    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
