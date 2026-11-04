"""Deterministic ICP scoring (0–100) with stored reasons, plus hard exclusions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.text import domain_of_email, normalise_text
from app.core.yamlconf import load_blocklist, load_industries
from app.db.models import Company, Contact, ResearchSnapshot
from app.db.models.enums import CompanyStatus, EmailStatus, RoleCategory, SizeHint
from app.services import audit, suppression
from app.services.contacts import contact_email

PROCUREMENT_ROLES = {
    RoleCategory.PROCUREMENT: 20,
    RoleCategory.PURCHASING: 18,
    RoleCategory.SUPPLY_CHAIN: 15,
    RoleCategory.OWNER_GM: 10,
    RoleCategory.OPERATIONS: 8,
    RoleCategory.FINANCE: 5,
    RoleCategory.GENERAL: 4,
    RoleCategory.UNKNOWN: 2,
}
SIZE_POINTS = {
    SizeHint.MEDIUM: 10,
    SizeHint.LARGE: 10,
    SizeHint.SMALL: 6,
    SizeHint.MICRO: 2,
    SizeHint.UNKNOWN: 0,
}
GOVERNMENT_MARKERS = ("ministry", "authority", "هيئة", "وزارة", "government", "municipality", "بلدية")


@dataclass
class QualificationResult:
    score: int
    reasons: list[dict[str, Any]] = field(default_factory=list)
    excluded_reason: str | None = None
    status: str = CompanyStatus.QUALIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "reasons": self.reasons,
            "excluded_reason": self.excluded_reason,
            "status": self.status,
        }


def hard_exclusion(company: Company, contacts: list[Contact], blocklist_domains: set[str]) -> str | None:
    if company.domain and company.domain.lower() in blocklist_domains:
        return "competitor_or_government_domain"
    name = normalise_text(company.name)
    if any(marker in name for marker in GOVERNMENT_MARKERS) and not contacts:
        return "government_or_tender_body"
    if not company.domain and not company.website:
        return "no_website"
    usable = [
        c
        for c in contacts
        if c.email_status not in (EmailStatus.INVALID, EmailStatus.DISPOSABLE) and c.email_hash
    ]
    if not usable:
        return "no_usable_contact"
    return None


def score_company(
    company: Company,
    contacts: list[Contact],
    snapshot: ResearchSnapshot | None,
) -> QualificationResult:
    industries = load_industries()
    result = QualificationResult(score=0)

    def add(points: int, reason: str, detail: Any = None) -> None:
        if points:
            result.score += points
            result.reasons.append({"reason": reason, "points": points, "detail": detail})

    playbook = industries.by_code(company.industry or "") if company.industry else None
    if playbook is not None:
        add(20, "industry_fit", playbook.code)
    else:
        add(0, "industry_unmatched", company.industry)

    extraction: dict[str, Any] = dict(snapshot.extracted) if snapshot else {}
    categories = extraction.get("likely_purchased_categories") or []
    if categories:
        points = min(15, 5 + 3 * len(categories))
        add(points, "evidence_of_buying", len(categories))

    add(SIZE_POINTS.get(SizeHint(company.size_hint), 0), "size_hint", company.size_hint)

    best_role_points = 0
    best_role = None
    for contact in contacts:
        points = PROCUREMENT_ROLES.get(RoleCategory(contact.role_category), 0)
        if points > best_role_points:
            best_role_points = points
            best_role = contact.role_category
    add(best_role_points, "procurement_contact", best_role)

    if company.website:
        add(5, "has_website", company.website)
    if company.language in ("ar", "mixed"):
        add(3, "arabic_site", company.language)

    zones = list(playbook.typical_zones) if playbook else []
    location = normalise_text(f"{company.city or ''} {company.industrial_zone or ''}")
    if any(normalise_text(zone) in location for zone in zones if zone):
        add(10, "target_zone", company.city or company.industrial_zone)

    signals = {str(s.get("signal")) for s in (extraction.get("procurement_signals") or [])}
    if "supplier_registration_page" in signals:
        add(7, "supplier_registration_page", True)
    if "erp_mention" in signals:
        add(4, "erp_mention", True)

    if any(c.email_status == EmailStatus.VALID for c in contacts):
        add(10, "verified_email", True)
    elif any(c.email_status == EmailStatus.RISKY for c in contacts):
        add(4, "risky_email", True)

    if extraction.get("do_not_contact_signals"):
        result.reasons.append({"reason": "research_do_not_contact_hint", "points": 0, "detail": True})

    result.score = max(0, min(100, result.score))
    return result


async def qualify_company(
    session: AsyncSession,
    *,
    company: Company,
    clock: Clock,
    settings: Settings,
) -> QualificationResult:
    del settings
    blocklist = load_blocklist()
    contacts = list(
        (await session.execute(select(Contact).where(Contact.company_id == company.id))).scalars().all()
    )
    excluded = hard_exclusion(company, contacts, blocklist.all_blocked_domains())
    if excluded is None:
        for contact in contacts:
            email = contact_email(contact)
            if email and await suppression.is_suppressed(session, email=email, phone=None):
                excluded = "already_suppressed"
                break
    if excluded is None and company.domain and domain_of_email(f"x@{company.domain}") in blocklist.free_mail:
        excluded = "free_mail_domain"
    snapshot = (
        await session.execute(
            select(ResearchSnapshot)
            .where(ResearchSnapshot.company_id == company.id)
            .order_by(ResearchSnapshot.version.desc())
            .limit(1)
        )
    ).scalars().first()
    if excluded is not None:
        company.excluded_reason = excluded
        company.status = CompanyStatus.EXCLUDED
        company.icp_score = 0
        company.icp_reasons = [{"reason": "excluded", "detail": excluded, "points": 0}]
        await session.flush()
        await audit.record(
            session,
            action="company_excluded",
            entity_type="company",
            entity_id=company.id,
            detail={"reason": excluded},
        )
        return QualificationResult(score=0, excluded_reason=excluded, status=CompanyStatus.EXCLUDED)

    result = score_company(company, contacts, snapshot)
    company.icp_score = result.score
    company.icp_reasons = result.reasons
    company.status = CompanyStatus.QUALIFIED
    company.excluded_reason = None
    await session.flush()
    await audit.emit_event(
        session,
        kind="company_qualified",
        entity_type="company",
        entity_id=company.id,
        payload={"score": result.score},
    )
    return result


async def qualify_pending(
    session: AsyncSession, *, clock: Clock, settings: Settings, limit: int = 200
) -> dict[str, Any]:
    rows = await session.execute(
        select(Company)
        .where(Company.status.in_([CompanyStatus.ENRICHED, CompanyStatus.RESEARCHED, CompanyStatus.DISCOVERED]))
        .limit(limit)
    )
    qualified = 0
    excluded = 0
    for company in rows.scalars().all():
        result = await qualify_company(session, company=company, clock=clock, settings=settings)
        if result.excluded_reason:
            excluded += 1
        else:
            qualified += 1
    return {"qualified": qualified, "excluded": excluded}


def utcnow() -> datetime:
    return datetime.now(UTC)
