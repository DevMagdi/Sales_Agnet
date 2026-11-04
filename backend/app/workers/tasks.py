"""Celery tasks: thin, idempotent wrappers over the services. Safe to run on two workers at once."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from app.adapters.registry import get_registry
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Campaign, Company, DiscoveryJob, InboundMessage, Mailbox, Reply
from app.db.models.enums import CampaignStatus, CompanyStatus, ReplyStatus
from app.db.session import session_scope
from app.services import (
    circuit_breakers,
    crawler,
    deliverability,
    enrichment,
    export_erase,
    handoff,
    imap_poller,
    mailboxes,
    qualification,
    reply_handler,
    reports,
    sender,
    sequencer,
    settings_service,
    verification,
)
from app.services import (
    research as research_service,
)
from app.workers.celery_app import celery_app

logger = get_logger("tasks")


def _run(coro: Any) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(coro)
    return result


async def _guard() -> tuple[bool, str]:
    async with session_scope() as session:
        allowed, reason = await settings_service.sending_allowed(session, SystemClock())
        return allowed, reason


@celery_app.task(name="outreach.send_due")
def send_due(limit: int = 25) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        async with session_scope() as session:
            allowed, reason = await settings_service.sending_allowed(session, clock)
            if not allowed:
                return {"skipped": reason}
            batch = await sender.send_due(
                session, registry=registry, clock=clock, settings=settings, limit=limit
            )
            return batch.as_dict()

    return _run(_inner())


@celery_app.task(name="outreach.compose_due")
def compose_due(limit: int = 25) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        async with session_scope() as session:
            batch = await sequencer.compose_due(
                session, registry=registry, clock=clock, settings=settings, limit=limit
            )
            return {
                "composed": batch.composed,
                "queued": batch.queued,
                "blocked": batch.blocked,
                "review": batch.review,
            }

    return _run(_inner())


@celery_app.task(name="outreach.plan_sequences")
def plan_sequences() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        clock = SystemClock()
        async with session_scope() as session:
            rows = await session.execute(select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE))
            created = 0
            for campaign in rows.scalars().all():
                result = await sequencer.plan_sequences(
                    session, campaign=campaign, clock=clock, settings=settings
                )
                created += result.created
            return {"created": created}

    return _run(_inner())


@celery_app.task(name="outreach.enrich_tick")
def enrich_tick(limit: int = 5) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        clock = SystemClock()
        crawler_instance = crawler.PoliteCrawler(settings)
        enriched = 0
        async with session_scope() as session:
            rows = await session.execute(
                select(Company)
                .where(Company.status == CompanyStatus.DISCOVERED, Company.website.is_not(None))
                .limit(limit)
            )
            for company in rows.scalars().all():
                result = await enrichment.enrich_company(
                    session, company=company, crawler=crawler_instance, clock=clock, settings=settings
                )
                enriched += 1 if result.pages else 0
        return {"enriched": enriched}

    return _run(_inner())


@celery_app.task(name="outreach.research_tick")
def research_tick(limit: int = 5) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        researched = 0
        async with session_scope() as session:
            rows = await session.execute(
                select(Company).where(Company.status == CompanyStatus.ENRICHED).limit(limit)
            )
            for company in rows.scalars().all():
                pages = await enrichment.cached_pages(session, company.id)
                if not pages:
                    continue
                await research_service.research_company(
                    session, company=company, pages=pages, adapter=registry.llm, clock=clock, settings=settings
                )
                researched += 1
        return {"researched": researched}

    return _run(_inner())


@celery_app.task(name="outreach.qualify_tick")
def qualify_tick(limit: int = 50) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        clock = SystemClock()
        async with session_scope() as session:
            return await qualification.qualify_pending(session, clock=clock, settings=settings, limit=limit)

    return _run(_inner())


@celery_app.task(name="outreach.discover_tick")
def discover_tick() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        from app.services import discovery as discovery_service

        async with session_scope() as session:
            rows = await session.execute(
                select(DiscoveryJob).where(DiscoveryJob.status == "pending").limit(3)
            )
            ran = 0
            for job in rows.scalars().all():
                await discovery_service.run_job(
                    session,
                    job=job,
                    search=registry.search,
                    places=registry.places,
                    clock=clock,
                    settings=settings,
                    industry_code=job.industry,
                )
                ran += 1
            return {"ran": ran}

    return _run(_inner())


@celery_app.task(name="outreach.poll_imap")
def poll_imap() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        ingested = 0
        handled = 0
        async with session_scope() as session:
            mailboxes_rows = await session.execute(select(Mailbox).where(Mailbox.status == "active"))
            for mailbox in mailboxes_rows.scalars().all():
                if not mailbox.imap_host:
                    continue
                parsed = await imap_poller.poll_mailbox(mailbox, settings=settings, clock=clock)
                for item in parsed:
                    inbound_row = await imap_poller.ingest(session, item.raw, clock=clock, settings=settings)
                    ingested += 1
                    if inbound_row.campaign_prospect_id:
                        await reply_handler.process_inbound(
                            session, inbound_row, registry=registry, clock=clock, settings=settings
                        )
                        handled += 1
        return {"ingested": ingested, "handled": handled}

    return _run(_inner())


@celery_app.task(name="outreach.handle_replies")
def handle_replies(limit: int = 25) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        handled = 0
        async with session_scope() as session:
            rows = await session.execute(
                select(Reply).where(Reply.status == ReplyStatus.NEW).limit(limit)
            )
            for reply in rows.scalars().all():
                inbound_row = await session.get(InboundMessage, reply.inbound_message_id)
                if inbound_row is None:
                    continue
                await reply_handler.handle(
                    session, reply, inbound_row, registry=registry, clock=clock, settings=settings
                )
                handled += 1
        return {"handled": handled}

    return _run(_inner())


@celery_app.task(name="outreach.breaker_check")
def breaker_check() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        async with session_scope() as session:
            verdict = await circuit_breakers.evaluate(
                session, registry=registry, clock=clock, settings=settings
            )
            return verdict.as_dict()

    return _run(_inner())


@celery_app.task(name="outreach.warmup_advance")
def warmup_advance() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        clock = SystemClock()
        async with session_scope() as session:
            return {"advanced": await mailboxes.advance_warmup(session, clock)}

    return _run(_inner())


@celery_app.task(name="outreach.retry_soft_bounces")
def retry_soft_bounces() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        clock = SystemClock()
        async with session_scope() as session:
            return {"retried": await sender.retry_soft_bounces(session, clock)}

    return _run(_inner())


@celery_app.task(name="outreach.daily_report")
def daily_report() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        registry = get_registry()
        clock = SystemClock()
        async with session_scope() as session:
            report = await reports.daily_report(session, clock, settings)
            doctor = await deliverability.run_doctor(
                session, settings=settings, registry=registry, clock=clock
            )
            await handoff.notify_staff(
                registry,
                title="Daily outreach report",
                body=(
                    f"Mode: {settings.outreach_mode}\n"
                    f"Sent: {report['kpis']['sent']} · Replies: {report['kpis']['replies']} · "
                    f"Demos: {report['kpis']['demos']}\n"
                    f"Bounce rate: {report['kpis']['bounce_rate']:.2%} · "
                    f"Cost today: ${report['kpis']['cost_today_usd']}\n"
                    f"Doctor: {'ok' if doctor.ok else 'FAILING: ' + ', '.join(c.name for c in doctor.checks if c.blocking and not c.ok)}"
                ),
                payload=report,
            )
            return {"sent": report["kpis"]["sent"], "doctor_ok": doctor.ok}

    return _run(_inner())


@celery_app.task(name="outreach.purge_retention")
def purge_retention() -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        settings = get_settings()
        clock = SystemClock()
        async with session_scope() as session:
            return await export_erase.purge_retention(session, settings=settings, clock=clock)

    return _run(_inner())


@celery_app.task(name="outreach.verify_contacts")
def verify_contacts(company_id: str) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        registry = get_registry()
        clock = SystemClock()
        async with session_scope() as session:
            summary = await verification.verify_company_contacts(session, company_id, registry.verify, clock)
            return summary.as_dict()

    return _run(_inner())
