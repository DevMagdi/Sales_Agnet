"""Enrichment: crawl a company site, store pages, extract contacts and re-crawl politely."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.text import detect_language, extract_emails
from app.db.models import Company, PageCache, ResearchSnapshot
from app.db.models.enums import CompanyStatus
from app.services import audit
from app.services import contacts as contact_service
from app.services.crawler import FetchedPage, PoliteCrawler, extract_contact_signals
from app.services.research import PageText

logger = get_logger("enrichment")


@dataclass
class EnrichmentResult:
    company_id: str
    pages: int
    contacts_created: int
    emails_found: int
    language: str
    skipped: list[dict[str, str]]
    new_contacts: list[str]


def website_of(company: Company) -> str:
    if company.website:
        return company.website
    if company.domain:
        return f"https://{company.domain}"
    return ""


def compress(text: str) -> str:
    import base64

    return base64.b64encode(zlib.compress(text.encode("utf-8"), 6)).decode("ascii")


def decompress(blob: str) -> str:
    import base64

    if not blob:
        return ""
    return zlib.decompress(base64.b64decode(blob.encode("ascii"))).decode("utf-8")


async def store_pages(session: AsyncSession, company_id: str, pages: list[FetchedPage], clock: Clock) -> None:
    import hashlib

    for page in pages:
        digest = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
        existing = await session.execute(
            select(PageCache).where(PageCache.company_id == company_id, PageCache.url == page.url)
        )
        row = existing.scalars().first()
        if row is None:
            row = PageCache(
                company_id=company_id,
                url=page.url,
                status_code=page.status_code,
                content_hash=digest,
                text_compressed=compress(page.text),
                fetched_at=clock.now(),
                final_url=page.final_url,
            )
            session.add(row)
        else:
            row.content_hash = digest
            row.status_code = page.status_code
            row.text_compressed = compress(page.text)
            row.fetched_at = clock.now()
            row.final_url = page.final_url
    await session.flush()


async def cached_pages(session: AsyncSession, company_id: str) -> list[PageText]:
    rows = await session.execute(select(PageCache).where(PageCache.company_id == company_id))
    return [PageText(url=row.url, text=decompress(row.text_compressed), status_code=row.status_code) for row in rows.scalars().all()]


async def enrich_company(
    session: AsyncSession,
    *,
    company: Company,
    crawler: PoliteCrawler | None = None,
    clock: Clock,
    settings: Settings,
    force: bool = False,
    max_pages: int | None = None,
) -> EnrichmentResult:
    website = website_of(company)
    if not website:
        company.status = CompanyStatus.DISCOVERED
        await session.flush()
        return EnrichmentResult(company.id, 0, 0, 0, company.language, [{"url": "", "reason": "no_website"}], [])
    if crawler is None:
        from app.services.crawler import PoliteCrawler

        crawler = PoliteCrawler(settings)
    if not force and not await crawler.is_stale(company.last_crawled_at, settings.crawl_recrawl_after_days):
        pages = await cached_pages(session, company.id)
        return EnrichmentResult(company.id, len(pages), 0, 0, company.language, [], [])

    result = await crawler.crawl_site(website, max_pages=max_pages)
    if not result.pages:
        await audit.record(
            session,
            action="crawl_failed",
            entity_type="company",
            entity_id=company.id,
            detail={"website": website, "skipped": result.skipped[:5]},
        )
        return EnrichmentResult(company.id, 0, 0, 0, company.language, result.skipped, [])

    await store_pages(session, company.id, result.pages, clock)
    signals = extract_contact_signals(result.pages)
    combined = "\n".join(p.text for p in result.pages)
    language = detect_language(combined[:20000])
    company.language = language if language != "unknown" else company.language

    new_ids: list[str] = []
    seen_emails: set[str] = set(signals["emails"])
    for item in signals["emails"]:
        contact = await contact_service.create_contact(
            session,
            company_id=company.id,
            email=item["email"],
            role_category=contact_service.classify_role(item["email"])[0],
            source_url=item["source_url"],
            preferred_language="ar" if language in ("ar", "mixed", "unknown") else "en",
            confidence=0.8,
        )
        if contact.id not in new_ids:
            new_ids.append(contact.id)
    for page in result.pages:
        for email in extract_emails(page.text):
            if email in seen_emails:
                continue
            seen_emails.add(email)
            contact = await contact_service.create_contact(
                session,
                company_id=company.id,
                email=email,
                source_url=page.url,
                preferred_language="ar" if language in ("ar", "mixed", "unknown") else "en",
                confidence=0.6,
            )
            new_ids.append(contact.id)

    company.last_crawled_at = clock.now()
    if company.status == CompanyStatus.DISCOVERED:
        company.status = CompanyStatus.ENRICHED
    await session.flush()
    await audit.emit_event(
        session,
        kind="company_enriched",
        entity_type="company",
        entity_id=company.id,
        payload={"pages": len(result.pages), "emails": len(seen_emails)},
    )
    logger.info("enriched", company_id=company.id, pages=len(result.pages), emails=len(seen_emails))
    return EnrichmentResult(
        company_id=company.id,
        pages=len(result.pages),
        contacts_created=len(new_ids),
        emails_found=len(seen_emails),
        language=language,
        skipped=result.skipped,
        new_contacts=new_ids,
    )


def page_texts_from_cache(rows: list[PageCache]) -> list[PageText]:
    return [PageText(url=row.url, text=decompress(row.text_compressed), status_code=row.status_code) for row in rows]


def latest_snapshot_meta(snapshot: ResearchSnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {}
    return {
        "version": snapshot.version,
        "model": snapshot.model,
        "cost_usd": snapshot.cost_usd,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }


def now() -> datetime:
    return datetime.now(UTC)
