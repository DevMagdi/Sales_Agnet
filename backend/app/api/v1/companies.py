"""Companies, contacts, CSV import/export and per-company actions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, or_, select

from app.api.deps import AdminUser, AnyUser, ClockDep, DbDep, RegistryDep, SettingsDep, WriterUser
from app.api.schemas import CompanyCreate, CompanyUpdate, ContactCreate, CsvCommitRequest, CsvPreviewRequest
from app.core.errors import NotFound, ValidationFailed
from app.db.models import (
    CampaignProspect,
    Company,
    Contact,
    OutreachMessage,
    Reply,
    ResearchSnapshot,
    Task,
)
from app.services import (
    audit,
    crawler,
    enrichment,
    export_erase,
    import_csv,
    qualification,
    verification,
)
from app.services import (
    contacts as contact_service,
)
from app.services import (
    research as research_service,
)

router = APIRouter(prefix="/companies", tags=["companies"])

EDITABLE = {"name", "name_ar", "city", "industrial_zone", "industry", "website", "notes"}


def company_view(company: Company, contacts_count: int = 0) -> dict[str, Any]:
    return {
        "id": company.id,
        "name": company.name,
        "name_ar": company.name_ar,
        "domain": company.domain,
        "website": company.website,
        "country": company.country,
        "city": company.city,
        "industrial_zone": company.industrial_zone,
        "industry": company.industry,
        "size_hint": company.size_hint,
        "language": company.language,
        "status": company.status,
        "icp_score": company.icp_score,
        "icp_reasons": company.icp_reasons,
        "excluded_reason": company.excluded_reason,
        "source": company.source,
        "source_ref": company.source_ref,
        "last_crawled_at": company.last_crawled_at.isoformat() if company.last_crawled_at else None,
        "contacts_count": contacts_count,
        "created_at": company.created_at.isoformat() if company.created_at else None,
    }


@router.get("")
async def list_companies(
    session: DbDep,
    user: AnyUser,
    q: str | None = None,
    status: str | None = None,
    industry: str | None = None,
    city: str | None = None,
    country: str | None = None,
    min_score: int | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
) -> dict[str, Any]:
    del user
    stmt = select(Company)
    count_stmt = select(func.count()).select_from(Company)
    filters = []
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(or_(Company.name.ilike(pattern), Company.name_ar.ilike(pattern), Company.domain.ilike(pattern)))
    if status:
        filters.append(Company.status == status)
    if industry:
        filters.append(Company.industry == industry)
    if city:
        filters.append(Company.city == city)
    if country:
        filters.append(Company.country == country)
    if min_score is not None:
        filters.append(Company.icp_score >= min_score)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)
    total = int((await session.execute(count_stmt)).scalar_one())
    rows = await session.execute(stmt.order_by(Company.icp_score.desc(), Company.created_at.desc())
                                 .limit(limit).offset(offset))
    companies = list(rows.scalars().all())
    counts: dict[str, int] = {}
    if companies:
        count_rows = await session.execute(
            select(Contact.company_id, func.count())
            .where(Contact.company_id.in_([c.id for c in companies]))
            .group_by(Contact.company_id)
        )
        counts = {cid: int(n) for cid, n in count_rows.all()}
    return {
        "items": [company_view(c, counts.get(c.id, 0)) for c in companies],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/export.csv")
async def export_companies_csv(session: DbDep, user: AnyUser, industry: str | None = None) -> Response:
    del user
    stmt = select(Company).order_by(Company.created_at.desc()).limit(5000)
    if industry:
        stmt = stmt.where(Company.industry == industry)
    companies = list((await session.execute(stmt)).scalars().all())
    csv_text = import_csv.export_companies_csv(companies)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="companies.csv"'},
    )


@router.post("")
async def create_company(payload: CompanyCreate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    company = Company(
        name=payload.name.strip(),
        domain=(payload.domain or "").strip().lower() or None,
        website=payload.website,
        country=payload.country,
        city=payload.city,
        industry=payload.industry,
        source="manual",
        source_ref=f"manual:{user.email}",
        status="discovered",
    )
    session.add(company)
    await session.flush()
    if payload.email:
        await contact_service.create_contact(
            session,
            company_id=company.id,
            email=payload.email,
            source_url=f"manual:{user.email}",
            confidence=0.9,
        )
    if payload.phone:
        await contact_service.create_contact(
            session, company_id=company.id, phone=payload.phone, phone_source_url=f"manual:{user.email}"
        )
    await audit.record(
        session, action="company_created", entity_type="company", entity_id=company.id, actor_id=user.id,
        actor_label=user.email,
    )
    return company_view(company)


@router.get("/{company_id}")
async def get_company(company_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    contacts = await contact_service.list_contacts(session, company.id)
    snapshot = (
        await session.execute(
            select(ResearchSnapshot)
            .where(ResearchSnapshot.company_id == company.id)
            .order_by(ResearchSnapshot.version.desc())
            .limit(1)
        )
    ).scalars().first()
    messages = await session.execute(
        select(OutreachMessage)
        .where(OutreachMessage.company_id == company.id)
        .order_by(OutreachMessage.created_at.desc())
        .limit(20)
    )
    prospects = await session.execute(
        select(CampaignProspect).where(CampaignProspect.company_id == company.id)
    )
    replies = await session.execute(
        select(Reply)
        .join(CampaignProspect, CampaignProspect.id == Reply.campaign_prospect_id)
        .where(CampaignProspect.company_id == company.id)
        .order_by(Reply.created_at.desc())
        .limit(20)
    )
    tasks = await session.execute(select(Task).where(Task.company_id == company.id).limit(20))
    return {
        "company": company_view(company, len(contacts)),
        "contacts": [contact_service.public_view(c) for c in contacts],
        "research": {
            "version": snapshot.version if snapshot else 0,
            "extracted": snapshot.extracted if snapshot else {},
            "evidence": snapshot.evidence if snapshot else [],
            "model": snapshot.model if snapshot else "",
            "cost_usd": snapshot.cost_usd if snapshot else 0.0,
        },
        "messages": [
            {
                "id": m.id,
                "step": m.step,
                "status": m.status,
                "subject": m.subject,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "blocked_reason": m.blocked_reason,
            }
            for m in messages.scalars().all()
        ],
        "prospects": [
            {"id": p.id, "campaign_id": p.campaign_id, "state": p.state, "step_index": p.step_index}
            for p in prospects.scalars().all()
        ],
        "replies": [
            {
                "id": r.id,
                "classification": r.classification,
                "confidence": r.confidence,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in replies.scalars().all()
        ],
        "tasks": [{"id": t.id, "type": t.type, "title": t.title, "status": t.status} for t in tasks.scalars().all()],
    }


@router.patch("/{company_id}")
async def update_company(company_id: str, payload: CompanyUpdate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    before = company_view(company)
    overrides = set(company.manual_overrides or [])
    for field_name in EDITABLE:
        value = getattr(payload, field_name, None)
        if value is not None:
            setattr(company, field_name, value)
            overrides.add(field_name)
    company.manual_overrides = sorted(overrides)
    if payload.exclude is True:
        company.excluded_reason = payload.excluded_reason or "manual_exclusion"
        company.status = "excluded"
    elif payload.exclude is False:
        company.excluded_reason = None
        company.status = "qualified" if company.icp_score else "discovered"
    await session.flush()
    await audit.record(
        session,
        action="company_updated",
        entity_type="company",
        entity_id=company.id,
        actor_id=user.id,
        actor_label=user.email,
        before=before,
        after=company_view(company),
    )
    return company_view(company)


@router.post("/{company_id}/contacts")
async def add_contact(company_id: str, payload: ContactCreate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    if not payload.email and not payload.phone:
        raise ValidationFailed("email or phone is required")
    contact = await contact_service.create_contact(
        session,
        company_id=company.id,
        email=payload.email,
        phone=payload.phone,
        full_name=payload.full_name,
        role_title=payload.role_title,
        source_url=payload.source_url or f"manual:{user.email}",
        confidence=0.9,
    )
    return contact_service.public_view(contact)


@router.post("/{company_id}/crawl")
async def crawl_company(
    company_id: str, session: DbDep, user: WriterUser, settings: SettingsDep, clock: ClockDep
) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    crawler_instance = crawler.PoliteCrawler(settings)
    result = await enrichment.enrich_company(
        session, company=company, crawler=crawler_instance, clock=clock, settings=settings, force=True
    )
    await audit.record(
        session,
        action="company_crawled",
        entity_type="company",
        entity_id=company.id,
        actor_id=user.id,
        actor_label=user.email,
        detail={"pages": result.pages, "emails": result.emails_found},
    )
    return {
        "pages": result.pages,
        "emails_found": result.emails_found,
        "contacts_created": result.contacts_created,
        "language": result.language,
        "skipped": result.skipped,
    }


@router.post("/{company_id}/research")
async def research_company(
    company_id: str,
    session: DbDep,
    user: WriterUser,
    settings: SettingsDep,
    clock: ClockDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    pages = await enrichment.cached_pages(session, company.id)
    if not pages:
        raise ValidationFailed("crawl the company before researching it")
    outcome = await research_service.research_company(
        session, company=company, pages=pages, adapter=registry.llm, clock=clock, settings=settings
    )
    await audit.record(
        session,
        action="company_researched_manual",
        entity_type="company",
        entity_id=company.id,
        actor_id=user.id,
        actor_label=user.email,
    )
    return {
        "cached": outcome.cached,
        "extraction": outcome.extraction,
        "evidence_ids": outcome.evidence_ids,
        "injection_flags": outcome.injection_flags,
        "snapshot_id": outcome.snapshot.id,
    }


@router.post("/{company_id}/qualify")
async def qualify(company_id: str, session: DbDep, user: WriterUser, settings: SettingsDep, clock: ClockDep) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    result = await qualification.qualify_company(session, company=company, clock=clock, settings=settings)
    await audit.record(
        session,
        action="company_qualified_manual",
        entity_type="company",
        entity_id=company.id,
        actor_id=user.id,
        actor_label=user.email,
        detail=result.as_dict(),
    )
    return result.as_dict()


@router.post("/{company_id}/verify-contacts")
async def verify_contacts(
    company_id: str, session: DbDep, user: WriterUser, clock: ClockDep, registry: RegistryDep
) -> dict[str, Any]:
    del user
    summary = await verification.verify_company_contacts(session, company_id, registry.verify, clock)
    return summary.as_dict()


@router.get("/{company_id}/export")
async def export_company(company_id: str, session: DbDep, user: AnyUser) -> dict[str, Any]:
    del user
    return await export_erase.export_company(session, company_id)


@router.delete("/{company_id}/erase")
async def erase_company(
    company_id: str, session: DbDep, user: AdminUser, clock: ClockDep
) -> dict[str, Any]:
    company = await session.get(Company, company_id)
    if company is None:
        raise NotFound("company not found")
    contacts = await contact_service.list_contacts(session, company.id)
    results = []
    for contact in contacts:
        results.append(
            await export_erase.erase_contact(session, contact.id, actor=user.email, clock=clock)
        )
    return {"erased_contacts": len(results)}


@router.post("/import/preview")
async def import_preview(payload: CsvPreviewRequest, user: WriterUser) -> dict[str, Any]:
    del user
    return import_csv.preview(payload.content, payload.mapping).as_dict()


@router.post("/import/commit")
async def import_commit(
    payload: CsvCommitRequest, session: DbDep, user: WriterUser, clock: ClockDep
) -> dict[str, Any]:
    report = await import_csv.commit_import(
        session,
        content=payload.content,
        mapping=payload.mapping,
        filename=payload.filename,
        actor=user.email,
        clock=clock,
    )
    if payload.save_mapping_as and payload.mapping:
        await import_csv.save_mapping(session, payload.save_mapping_as, payload.mapping)
    return report
