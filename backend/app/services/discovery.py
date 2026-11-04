"""Discovery: official Places/Search APIs behind adapters, deduped into `companies`.

Query templates come from the industry playbooks (Arabic + English). Only organic results that
look like company sites are kept; marketplaces/directories are skipped unless the job is a
configured directory job. Cost is metered into api_costs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import PlaceResult, PlacesAdapter, SearchAdapter, SearchResult
from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.text import normalise_phone, normalise_space
from app.core.yamlconf import IndustryPlaybook, load_industries
from app.db.models import Company, DiscoveryJob
from app.db.models.enums import CompanyStatus, SourceKind
from app.services import audit, budgets
from app.services.import_csv import DIRECTORIES, domain_from_website, find_company_by_domain

logger = get_logger("discovery")

QUERY_TEMPLATES = {
    "ar": ["مصنع {category_ar} {zone}", "شركة {category_ar} {zone}", "مصانع {category_ar} في {zone}"],
    "en": ["{category_en} factory {zone}", "{category_en} manufacturer {zone}", "{category_en} company {zone}"],
}


@dataclass
class DiscoveryOutcome:
    job_id: str | None
    found: int = 0
    created: int = 0
    duplicates: int = 0
    skipped: int = 0
    cost_usd: float = 0.0
    companies: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "found": self.found,
            "created": self.created,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "cost_usd": round(self.cost_usd, 4),
            "companies": self.companies,
            "details": self.details,
        }


def build_queries(
    playbook: IndustryPlaybook, zones: list[str] | None = None, country: str = "EG"
) -> list[str]:
    zone_list = zones or playbook.typical_zones[:3] or (["Cairo"] if country == "EG" else ["Riyadh"])
    queries: list[str] = []
    for zone in zone_list:
        for template in QUERY_TEMPLATES["ar"]:
            queries.append(template.format(category_ar=playbook.name_ar, zone=zone))
        for template in QUERY_TEMPLATES["en"]:
            queries.append(template.format(category_en=playbook.name_en, zone=zone))
    return queries


def looks_like_company_site(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for blocked in DIRECTORIES:
        if host == blocked or host.endswith("." + blocked):
            return False
    return not any(host.endswith(suffix) for suffix in (".pdf", ".jpg", ".png"))


def name_from_result(result: SearchResult) -> tuple[str, str]:
    title = normalise_space(result.title)
    name = title.split("|")[0].split("-")[0].split("—")[0].strip() or title
    return name[:300], domain_from_website(result.url)


async def create_company(
    session: AsyncSession,
    *,
    name: str,
    domain: str,
    website: str = "",
    country: str = "EG",
    city: str | None = None,
    industry: str | None = None,
    source: str = SourceKind.SEARCH,
    source_ref: str = "",
    place_id: str | None = None,
    phone: str = "",
    lat: float | None = None,
    lng: float | None = None,
) -> tuple[Company, bool]:
    if domain:
        existing = await find_company_by_domain(session, domain)
        if existing is not None:
            return existing, False
    company = Company(
        name=name,
        domain=domain or None,
        website=website or (f"https://{domain}" if domain else None),
        country=country or "EG",
        city=city,
        industry=industry,
        source=source,
        source_ref=source_ref or place_id or "",
        place_id=place_id,
        lat=lat,
        lng=lng,
        status=CompanyStatus.DISCOVERED,
        language="unknown",
    )
    session.add(company)
    await session.flush()
    if phone:
        from app.services import contacts as contact_service

        await contact_service.create_contact(
            session,
            company_id=company.id,
            phone=normalise_phone(phone),
            phone_source_url=source_ref or "discovery",
            confidence=0.5,
        )
    return company, True


async def run_search_job(
    session: AsyncSession,
    *,
    adapter: SearchAdapter,
    job: DiscoveryJob,
    playbook: IndustryPlaybook,
    clock: Clock,
    settings: Settings,
) -> DiscoveryOutcome:
    outcome = DiscoveryOutcome(job_id=job.id)
    job.status = "running"
    queries = build_queries(playbook, [job.city] if job.city else None, job.country)
    for query in queries:
        if outcome.cost_usd >= job.budget_cap_usd:
            outcome.details.append({"query": query, "skipped": "budget_cap_reached"})
            continue
        await budgets.check(session, clock, settings)
        results = await adapter.search(query, num_results=min(job.max_results, 20))
        cost = float(getattr(adapter, "cost_per_call_usd", 0.0) or 0.0)
        if cost:
            await budgets.record_api_cost(
                session, provider=f"search:{adapter.name}", cost_usd=cost, clock=clock, meta={"query": query}
            )
            outcome.cost_usd += cost
        outcome.found += len(results)
        for result in results:
            if not looks_like_company_site(result.url):
                outcome.skipped += 1
                continue
            name, domain = name_from_result(result)
            if not name or not domain:
                outcome.skipped += 1
                continue
            company, created = await create_company(
                session,
                name=name,
                domain=domain,
                website=result.url,
                country=job.country,
                city=job.city,
                industry=playbook.code,
                source=SourceKind.SEARCH,
                source_ref=query,
            )
            if created:
                outcome.created += 1
                outcome.companies.append(company.id)
            else:
                outcome.duplicates += 1
    job.status = "completed"
    job.found = outcome.found
    job.created_count = outcome.created
    job.duplicates = outcome.duplicates
    job.cost_usd = outcome.cost_usd
    job.report = outcome.as_dict()
    await session.flush()
    await audit.record(
        session,
        action="discovery_completed",
        entity_type="discovery_job",
        entity_id=job.id,
        detail={"created": outcome.created, "duplicates": outcome.duplicates, "cost": outcome.cost_usd},
    )
    return outcome


async def run_places_job(
    session: AsyncSession,
    *,
    adapter: PlacesAdapter,
    job: DiscoveryJob,
    playbook: IndustryPlaybook,
    clock: Clock,
    settings: Settings,
) -> DiscoveryOutcome:
    outcome = DiscoveryOutcome(job_id=job.id)
    job.status = "running"
    query = f"{playbook.name_ar} {job.city or ''} {job.country}".strip()
    await budgets.check(session, clock, settings)
    places: list[PlaceResult] = await adapter.text_search(query, max_results=job.max_results)
    cost = float(getattr(adapter, "cost_per_call_usd", 0.0) or 0.0)
    if cost:
        await budgets.record_api_cost(
            session, provider=f"places:{adapter.name}", cost_usd=cost, clock=clock, meta={"query": query}
        )
        outcome.cost_usd += cost
    outcome.found = len(places)
    for place in places:
        domain = domain_from_website(place.website) if place.website else ""
        if not domain:
            outcome.skipped += 1
            outcome.details.append({"name": place.name, "skipped": "no_website"})
            continue
        company, created = await create_company(
            session,
            name=place.name or domain,
            domain=domain,
            website=place.website,
            country=job.country,
            city=place.city or job.city,
            industry=playbook.code,
            source=SourceKind.PLACES,
            source_ref=place.place_id or query,
            place_id=place.place_id,
            phone=place.phone,
            lat=place.lat,
            lng=place.lng,
        )
        if created:
            outcome.created += 1
            outcome.companies.append(company.id)
        else:
            outcome.duplicates += 1
    job.status = "completed"
    job.created_count = outcome.created
    job.duplicates = outcome.duplicates
    job.found = outcome.found
    job.cost_usd = outcome.cost_usd
    job.report = outcome.as_dict()
    await session.flush()
    return outcome


async def run_job(
    session: AsyncSession,
    *,
    job: DiscoveryJob,
    search: SearchAdapter,
    places: PlacesAdapter,
    clock: Clock,
    settings: Settings,
    industry_code: str | None = None,
) -> DiscoveryOutcome:
    industries = load_industries()
    playbook = industries.by_code(industry_code or job.industry or "") or (
        industries.industries[0] if industries.industries else None
    )
    if playbook is None:
        job.status = "failed"
        job.report = {"error": "no_playbook"}
        await session.flush()
        return DiscoveryOutcome(job_id=job.id, details=[{"error": "no_playbook"}])
    if job.kind == "places":
        return await run_places_job(
            session, adapter=places, job=job, playbook=playbook, clock=clock, settings=settings
        )
    return await run_search_job(
        session, adapter=search, job=job, playbook=playbook, clock=clock, settings=settings
    )


async def approve_to_pipeline(session: AsyncSession, job_id: str, clock: Clock) -> dict[str, Any]:
    job = await session.get(DiscoveryJob, job_id)
    if job is None:
        return {"error": "job_not_found"}
    ids = list(job.report.get("companies", []))
    approved = 0
    for company_id in ids:
        company = await session.get(Company, company_id)
        if company is not None and company.status == CompanyStatus.DISCOVERED:
            approved += 1
    await audit.record(
        session,
        action="discovery_approved",
        entity_type="discovery_job",
        entity_id=job_id,
        detail={"approved": approved},
    )
    return {"approved": approved, "company_ids": ids}


async def list_jobs(session: AsyncSession, limit: int = 50) -> list[DiscoveryJob]:
    rows = await session.execute(select(DiscoveryJob).order_by(DiscoveryJob.created_at.desc()).limit(limit))
    return list(rows.scalars().all())
