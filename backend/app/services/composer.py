"""Message composition: template + two LLM slots + claim check + send gate.

The model never writes a whole email. It writes at most the opening observation; everything else
comes from the versioned template. A claim failure triggers exactly one regeneration with the
failure reason; a second failure drops personalisation and sends the static template variant.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import LLMAdapter
from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.crypto import sign_token
from app.core.errors import ConfigError, NotFound
from app.core.logging import get_logger
from app.core.text import mask_email
from app.core.yamlconf import ClaimsConfig, load_claims
from app.db.models import (
    Campaign,
    CampaignProspect,
    Company,
    Contact,
    Mailbox,
    OutreachMessage,
    ResearchSnapshot,
)
from app.db.models.enums import (
    CampaignStatus,
    Channel,
    MessageStatus,
    ProspectState,
    TemplateStep,
)
from app.services import (
    claim_check,
    llm_client,
    personalization,
    send_gate,
    state_machine,
    templates,
)
from app.services import (
    mailboxes as mailbox_service,
)
from app.services.contacts import contact_email
from app.services.schemas import ClaimVerification

logger = get_logger("composer")

BREAKUP_AFTER_FOLLOWUPS = True
CLAIM_RETRY_LIMIT = 1


@dataclass
class ComposeResult:
    message: OutreachMessage | None
    status: str
    gate_report: dict[str, Any] | None = None
    claim_report: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def unsubscribe_url_for(contact_id: str, campaign_id: str | None, base_url: str, ttl_days: int) -> str:
    token = sign_token(
        {"c": contact_id, "k": campaign_id or "none", "p": "unsub"},
        ttl_seconds=ttl_days * 86400,
    )
    return f"{base_url.rstrip('/')}/u/{token}"


def whatsapp_link(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"https://wa.me/{digits}" if digits else ""


def greeting_for(contact: Contact | None, company: Company, language: str) -> str:
    name = (contact.full_name or "").strip() if contact else ""
    if name:
        return f"أستاذ/ة {name}" if language == "ar" else f"Dear {name}"
    if language == "ar":
        return f"السادة / فريق المشتريات في {company.name}"
    return f"Dear Procurement team at {company.name}"


def build_variables(
    *,
    company: Company,
    contact: Contact | None,
    campaign: Campaign | None,
    claims: ClaimsConfig,
    settings: Settings,
    language: str,
    personalisation: personalization.Personalisation,
    unsubscribe_url: str,
    original_subject: str = "",
    referred_name: str = "",
) -> dict[str, Any]:
    poc = settings.poc_offer_text_ar if language == "ar" else settings.poc_offer_text_en
    demo_minutes = settings.demo_duration_minutes
    poc_text = poc.format(duration=demo_minutes) if poc else ""
    return {
        "greeting": greeting_for(contact, company, language),
        "company_name": company.name,
        "category": personalisation.category,
        "opening_observation": personalisation.opening_observation,
        "fact_intake": claims.fact("intake", language),
        "fact_comparison": claims.fact("comparison", language),
        "fact_control": claims.fact("control", language),
        "poc_offer_text": poc_text,
        "features_url": settings.features_url,
        "whatsapp_link": whatsapp_link(settings.company_phone or ""),
        "sender_name": settings.sender_name,
        "sender_title": settings.sender_title,
        "company_address": settings.company_address,
        "company_phone": settings.company_phone,
        "unsubscribe_url": unsubscribe_url,
        "example_items": personalisation.example_items,
        "original_subject": original_subject or "",
        "booking_url": settings.booking_url,
        "contact_name": (contact.full_name if contact and contact.full_name else greeting_for(contact, company, language)),
        "demo_duration": demo_minutes,
        "referred_name": referred_name,
        "campaign_name": campaign.name if campaign is not None else "",
    }


def step_for_prospect(prospect: CampaignProspect, campaign: Campaign) -> str:
    index = prospect.step_index
    if index == 0:
        return TemplateStep.INITIAL
    if index == 1:
        return TemplateStep.FOLLOWUP_1
    if index == 2 and campaign.max_followups >= 2:
        return TemplateStep.FOLLOWUP_2
    if BREAKUP_AFTER_FOLLOWUPS:
        return TemplateStep.BREAKUP
    return TemplateStep.FOLLOWUP_2


async def _latest_research(session: AsyncSession, company_id: str) -> ResearchSnapshot | None:
    result = await session.execute(
        select(ResearchSnapshot)
        .where(ResearchSnapshot.company_id == company_id)
        .order_by(ResearchSnapshot.version.desc())
        .limit(1)
    )
    return result.scalars().first()


async def verify_claims(
    session: AsyncSession,
    *,
    adapter: LLMAdapter,
    clock: Clock,
    settings: Settings,
    subject: str,
    body: str,
    claims: ClaimsConfig,
    evidence: list[dict[str, Any]],
    approved_phrases: list[str],
) -> tuple[claim_check.ClaimCheckResult, str | None]:
    allowed_hosts = claim_check.default_allowed_hosts(settings)
    issues = claim_check.run_deterministic(
        body,
        subject=subject,
        allowed_hosts=allowed_hosts,
        approved_phrases=approved_phrases,
        claims=claims,
    )
    if issues:
        return (
            claim_check.ClaimCheckResult(passed=False, issues=issues, reason="deterministic_claims"),
            None,
        )
    prompt_body, prompt_version = llm_client.load_prompt("claim_verify", "v1")
    user_prompt = claim_check.build_verify_prompt(body, subject, claims, evidence)
    try:
        response = await llm_client.call_json(
            session,
            adapter,
            task="claim_verify",
            system=prompt_body,
            user=user_prompt,
            clock=clock,
            settings=settings,
            schema=ClaimVerification,
            model_tier="fast",
            max_tokens=1200,
            temperature=0.0,
            prompt_version=prompt_version,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed: unverified message is not sent
        logger.warning("claim_verifier_unavailable", error=str(exc)[:200])
        return (
            claim_check.ClaimCheckResult(
                passed=False, reason="llm_verifier_unavailable", llm_verdict={"error": str(exc)[:200]}
            ),
            None,
        )
    verdict = ClaimVerification.model_validate(response.data)
    unsupported = [s for s in verdict.statements if not s.supported]
    issues = [
        claim_check.ClaimIssue(rule="unsupported_statement", snippet=s.text[:120], detail=s.source)
        for s in unsupported
    ]
    passed = verdict.verdict == "pass" and not unsupported
    return (
        claim_check.ClaimCheckResult(
            passed=passed,
            issues=issues,
            llm_verdict=verdict.model_dump(),
            llm_call_id=response.call.id,
            reason="" if passed else "llm_verifier_rejected",
        ),
        response.call.id,
    )


async def compose_for_prospect(
    session: AsyncSession,
    *,
    prospect: CampaignProspect,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
    step: str | None = None,
    claims: ClaimsConfig | None = None,
) -> ComposeResult:
    campaign = await session.get(Campaign, prospect.campaign_id)
    company = await session.get(Company, prospect.company_id)
    if campaign is None or company is None:
        raise NotFound("campaign or company missing for prospect")
    contact = await session.get(Contact, prospect.contact_id) if prospect.contact_id else None
    if contact is None:
        return ComposeResult(message=None, status="blocked", notes=["no_contact"])

    claims_config = claims or load_claims()
    snapshot = await _latest_research(session, company.id)
    extraction: dict[str, Any] = dict(snapshot.extracted) if snapshot else {}
    evidence_items: list[dict[str, Any]] = list(snapshot.evidence) if snapshot else []

    personalisation = await personalization.personalise(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        extraction=extraction,
        adapter=registry.llm,
        clock=clock,
        settings=settings,
    )

    chosen_step = step or step_for_prospect(prospect, campaign)
    template = await templates.get_active(
        session, step=chosen_step, locale=personalisation.language, code=campaign.template_set
    )
    if template is None:
        template = await templates.get_active(session, step=chosen_step, locale=personalisation.language)
    if template is None:
        raise NotFound(f"no active template for step={chosen_step} locale={personalisation.language}")

    unsub = unsubscribe_url_for(
        contact.id, campaign.id, settings.public_base_url, settings.unsubscribe_token_ttl_days
    )
    approved_phrases = [settings.poc_offer_text_ar, settings.poc_offer_text_en]
    variables = build_variables(
        company=company,
        contact=contact,
        campaign=campaign,
        claims=claims_config,
        settings=settings,
        language=personalisation.language,
        personalisation=personalisation,
        unsubscribe_url=unsub,
    )
    variables = {k: ("" if v is None else v) for k, v in variables.items()}

    subject = templates.render(template.subject, variables)
    body = templates.render(template.body_text, variables)
    claim_result, call_id = await verify_claims(
        session,
        adapter=registry.llm,
        clock=clock,
        settings=settings,
        subject=subject,
        body=body,
        claims=claims_config,
        evidence=evidence_items,
        approved_phrases=[p for p in approved_phrases if p],
    )
    notes: list[str] = []
    if not claim_result.passed and chosen_step in (TemplateStep.INITIAL, TemplateStep.FOLLOWUP_1):
        notes.append("claim_retry_with_hedged_slot")
        hedged = await personalization.personalise(
            session,
            company=company,
            contact=contact,
            campaign=campaign,
            extraction={**extraction, "likely_purchased_categories": []},
            adapter=registry.llm,
            clock=clock,
            settings=settings,
        )
        variables["opening_observation"] = hedged.opening_observation
        subject = templates.render(template.subject, variables)
        body = templates.render(template.body_text, variables)
        claim_result, call_id = await verify_claims(
            session,
            adapter=registry.llm,
            clock=clock,
            settings=settings,
            subject=subject,
            body=body,
            claims=claims_config,
            evidence=evidence_items,
            approved_phrases=[p for p in approved_phrases if p],
        )
        if not claim_result.passed:
            notes.append("personalisation_dropped")
            variables["opening_observation"] = ""
            subject = templates.render(template.subject, variables)
            body = templates.render(template.body_text, variables)
            claim_result, call_id = await verify_claims(
                session,
                adapter=registry.llm,
                clock=clock,
                settings=settings,
                subject=subject,
                body=body,
                claims=claims_config,
                evidence=evidence_items,
                approved_phrases=[p for p in approved_phrases if p],
            )

    mailbox = await mailbox_service.pick_mailbox(
        session, pool_id=campaign.mailbox_pool_id, clock=clock, tz=campaign.timezone
    )
    if mailbox is None:
        fallback = await session.execute(select(Mailbox).limit(1))
        mailbox = fallback.scalars().first()

    idem = send_gate.idempotency_key(prospect.id, chosen_step, Channel.EMAIL)
    existing_message = await session.execute(
        select(OutreachMessage).where(OutreachMessage.idempotency_key == idem)
    )
    message = existing_message.scalars().first()
    if message is None:
        message = OutreachMessage(
            campaign_prospect_id=prospect.id,
            company_id=company.id,
            contact_id=contact.id,
            channel=Channel.EMAIL,
            direction="outbound",
            step=chosen_step,
            locale=personalisation.language,
            subject=subject,
            body_text=body,
            status=MessageStatus.DRAFT,
            mailbox_id=mailbox.id if mailbox else None,
            template_code=template.code,
            template_version=template.version,
            idempotency_key=idem,
            to_email_hash=contact.email_hash,
            to_domain=contact_email(contact).partition("@")[2] or None,
            sender_email=mailbox.from_email if mailbox else settings.sender_email,
            evidence_ids=[personalisation.evidence_id] if personalisation.evidence_id else [],
            llm_call_ids=[c for c in [personalisation.llm_call_id, call_id] if c],
            word_count=len(body.split()),
        )
        session.add(message)
    else:
        message.subject = subject
        message.body_text = body
        message.locale = personalisation.language
        message.word_count = len(body.split())
    message.claim_report = claim_result.as_report()
    await session.flush()

    ctx = await send_gate.build_context(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        prospect=prospect,
        mailbox=mailbox,
        message=message,
        channel=Channel.EMAIL,
        step=chosen_step,
        locale=personalisation.language,
        subject=subject,
        body_text=body,
        unsubscribe_url=unsub,
        settings=settings,
        clock=clock,
        claims_report=claim_result.as_report(),
        claims=claims_config,
    )
    report = await send_gate.evaluate(session, ctx, clock, settings)
    message.check_report = report.as_dict()

    review_required = False
    if campaign.review_first_n > 0:
        sent_count = await session.execute(
            select(OutreachMessage).where(
                OutreachMessage.campaign_prospect_id == prospect.id,
                OutreachMessage.sent_at.is_not(None),
            )
        )
        if len(list(sent_count.scalars().all())) < campaign.review_first_n:
            review_required = True
    mode = ctx.mode
    if not report.passed:
        message.status = MessageStatus.BLOCKED
        message.blocked_reason = (report.blocked_reason or "blocked")[:190]
        status = "blocked"
        if not report.permanent and prospect.state == ProspectState.COMPOSING:
            from app.services.state_machine import is_legal

            if is_legal(prospect.state, ProspectState.QUEUED):
                await state_machine.transition(
                    session, prospect, ProspectState.QUEUED, clock=clock, reason="transient_block"
                )
            retry_at = None
            for check in report.failures:
                if check.detail.get("next_start"):
                    from datetime import datetime

                    try:
                        retry_at = datetime.fromisoformat(str(check.detail["next_start"]))
                    except ValueError:
                        retry_at = None
                    break
                if check.detail.get("retry_in_hours"):
                    retry_at = clock.now() + timedelta(hours=int(check.detail["retry_in_hours"]))
                    break
            prospect.next_action_at = retry_at or (clock.now() + timedelta(hours=6))
    elif review_required:
        message.status = MessageStatus.NEEDS_REVIEW
        status = "needs_review"
    elif mode == "dry_run":
        message.status = MessageStatus.WOULD_SEND
        message.scheduled_for = clock.now()
        status = "would_send"
    else:
        message.status = MessageStatus.QUEUED
        message.scheduled_for = clock.now() + timedelta(seconds=1)
        status = "queued"
    if prospect.state in (ProspectState.QUALIFIED, ProspectState.QUEUED, ProspectState.BLOCKED_BY_GATE):
        if status in ("queued", "would_send"):
            await state_machine.transition(
                session, prospect, ProspectState.COMPOSING, clock=clock, reason="composed"
            )
        elif status == "blocked" and report.permanent:
            await state_machine.transition(
                session,
                prospect,
                ProspectState.BLOCKED_BY_GATE,
                clock=clock,
                reason=message.blocked_reason or "blocked",
            )
    await session.flush()
    return ComposeResult(
        message=message,
        status=status,
        gate_report=report.as_dict(),
        claim_report=claim_result.as_report(),
        notes=notes,
    )


async def preview_message(
    session: AsyncSession,
    *,
    campaign: Campaign,
    company: Company,
    contact: Contact,
    step: str,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
) -> dict[str, Any]:
    """Dashboard preview: render + full gate in check-only mode. Sends nothing, stores nothing."""
    claims = load_claims()
    snapshot = await _latest_research(session, company.id)
    extraction = dict(snapshot.extracted) if snapshot else {}
    personalisation = await personalization.personalise(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        extraction=extraction,
        adapter=registry.llm,
        clock=clock,
        settings=settings,
    )
    template = await templates.get_active(session, step=step, locale=personalisation.language)
    if template is None:
        raise NotFound(f"no template for step={step}")
    unsub = unsubscribe_url_for(
        contact.id, campaign.id, settings.public_base_url, settings.unsubscribe_token_ttl_days
    )
    variables = build_variables(
        company=company,
        contact=contact,
        campaign=campaign,
        claims=claims,
        settings=settings,
        language=personalisation.language,
        personalisation=personalisation,
        unsubscribe_url=unsub,
    )
    variables = {k: ("" if v is None else v) for k, v in variables.items()}
    subject = templates.render(template.subject, variables)
    body = templates.render(template.body_text, variables)
    claim_result, _ = await verify_claims(
        session,
        adapter=registry.llm,
        clock=clock,
        settings=settings,
        subject=subject,
        body=body,
        claims=claims,
        evidence=list(snapshot.evidence) if snapshot else [],
        approved_phrases=[p for p in (settings.poc_offer_text_ar, settings.poc_offer_text_en) if p],
    )
    prospect = (
        await session.execute(
            select(CampaignProspect).where(
                CampaignProspect.campaign_id == campaign.id, CampaignProspect.company_id == company.id
            )
        )
    ).scalars().first()
    mailbox = await mailbox_service.pick_mailbox(session, pool_id=campaign.mailbox_pool_id, clock=clock)
    ctx = await send_gate.build_context(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        prospect=prospect,
        mailbox=mailbox,
        message=None,
        channel=Channel.EMAIL,
        step=step,
        locale=personalisation.language,
        subject=subject,
        body_text=body,
        unsubscribe_url=unsub,
        settings=settings,
        clock=clock,
        claims_report=claim_result.as_report(),
        claims=claims,
    )
    ctx.check_only = True
    report = await send_gate.evaluate(session, ctx, clock, settings)
    return {
        "subject": subject,
        "body_text": body,
        "locale": personalisation.language,
        "category": personalisation.category,
        "opening_style": personalisation.style,
        "evidence": {"id": personalisation.evidence_id, "url": personalisation.evidence_url},
        "template": {"code": template.code, "version": template.version},
        "claim_report": claim_result.as_report(),
        "gate_report": report.as_dict(),
        "notes": personalisation.notes,
        "to_masked": mask_email(contact_email(contact)),
    }


def content_hash_for(message: OutreachMessage) -> str:
    raw = f"{message.subject}\n{message.body_text}".encode()
    return hashlib.sha256(raw).hexdigest()


def require_identity(settings: Settings) -> None:
    missing = [
        name
        for name, value in (
            ("SENDER_NAME", settings.sender_name),
            ("SENDER_TITLE", settings.sender_title),
            ("COMPANY_ADDRESS", settings.company_address),
            ("COMPANY_PHONE", settings.company_phone),
            ("FEATURES_URL", settings.features_url),
            ("POC_OFFER_TEXT_AR", settings.poc_offer_text_ar),
            ("POC_OFFER_TEXT_EN", settings.poc_offer_text_en),
        )
        if not value
    ]
    if missing:
        raise ConfigError("sender identity is incomplete for composition", details={"missing": missing})


def json_summary(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)[:500]


async def campaign_is_active(session: AsyncSession, campaign_id: str) -> bool:
    campaign = await session.get(Campaign, campaign_id)
    return campaign is not None and campaign.status == CampaignStatus.ACTIVE
