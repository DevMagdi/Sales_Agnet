"""Reply policy table (code, not the model) and its execution.

Class → action. Everything that might send a message still goes through the Send Gate; everything
that must not be automated becomes a human task with a ready draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.yamlconf import load_claims
from app.db.models import (
    Campaign,
    CampaignProspect,
    Company,
    Contact,
    InboundMessage,
    OutreachMessage,
    Reply,
)
from app.db.models.enums import (
    MessageStatus,
    ProspectState,
    ReplyClass,
    ReplyStatus,
    SuppressionReason,
    TaskType,
)
from app.services import (
    audit,
    consent,
    handoff,
    llm_client,
    reply_classifier,
    sender,
    state_machine,
    suppression,
    templates,
)
from app.services.contacts import contact_email, contact_phone, create_contact
from app.services.inbound import recent_thread
from app.services.schemas import DraftedReply

logger = get_logger("reply_handler")

DEFAULT_AUTO_REPLY_CLASSES = ["unsubscribe", "wants_info", "meeting_proposal"]
SNOOZE_DAYS = 60
OOO_DEFAULT_DAYS = 7
TERMINAL_FORCE_STATES = {
    ProspectState.DO_NOT_CONTACT,
    ProspectState.CLOSED_LOST,
    ProspectState.BOUNCED,
    ProspectState.NO_RESPONSE,
    ProspectState.LOST,
    ProspectState.WON,
    ProspectState.REFERRAL_FOLLOWED,
    ProspectState.EXCLUDED,
}


@dataclass
class HandleResult:
    reply_class: str
    action: str
    auto_message_id: str | None = None
    task_id: str | None = None
    demo_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reply_class": self.reply_class,
            "action": self.action,
            "auto_message_id": self.auto_message_id,
            "task_id": self.task_id,
            "demo_id": self.demo_id,
            "notes": self.notes,
        }


def auto_classes(campaign: Campaign | None) -> list[str]:
    if campaign is not None and campaign.auto_reply_classes:
        return list(campaign.auto_reply_classes)
    return list(DEFAULT_AUTO_REPLY_CLASSES)


async def _force_state(
    session: AsyncSession, prospect: CampaignProspect | None, target: str, *, clock: Clock, reason: str
) -> None:
    if prospect is None:
        return
    if state_machine.is_legal(prospect.state, target):
        await state_machine.transition(session, prospect, target, clock=clock, reason=reason)
        return
    # always reachable safety terminals
    if target in TERMINAL_FORCE_STATES:
        previous = prospect.state
        prospect.state = target
        prospect.next_action_at = None
        prospect.locked_until = None
        await session.flush()
        await audit.record(
            session,
            action="state_forced",
            entity_type="campaign_prospect",
            entity_id=prospect.id,
            detail={"from": previous, "to": target, "reason": reason},
        )


async def _cancel_pending(session: AsyncSession, prospect_id: str, reason: str) -> int:
    rows = await session.execute(
        select(OutreachMessage).where(
            OutreachMessage.campaign_prospect_id == prospect_id,
            OutreachMessage.status.in_([MessageStatus.QUEUED, MessageStatus.NEEDS_REVIEW]),
        )
    )
    count = 0
    for message in rows.scalars().all():
        message.status = MessageStatus.BLOCKED
        message.blocked_reason = reason[:190]
        count += 1
    await session.flush()
    return count


async def _send_auto_reply(
    session: AsyncSession,
    *,
    prospect: CampaignProspect | None,
    company: Company,
    contact: Contact,
    inbound: InboundMessage,
    step: str,
    locale: str,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
    variables_extra: dict[str, Any] | None = None,
) -> str | None:
    template = await templates.get_active(session, step=step, locale=locale)
    if template is None:
        logger.warning("auto_reply_template_missing", step=step, locale=locale)
        return None
    from app.services.composer import build_variables, unsubscribe_url_for, whatsapp_link  # noqa: F401

    claims = load_claims()
    campaign = await session.get(Campaign, prospect.campaign_id) if prospect else None
    original_subject = (inbound.subject or "").strip() or "ProcureAI"
    unsub = unsubscribe_url_for(
        contact.id, campaign.id if campaign else None, settings.public_base_url, settings.unsubscribe_token_ttl_days
    )
    from app.services import personalization

    dummy = personalization.Personalisation(
        language=locale, category="", opening_observation="", style="hedged"
    )
    variables = build_variables(
        company=company,
        contact=contact,
        campaign=campaign,
        claims=claims,
        settings=settings,
        language=locale,
        personalisation=dummy,
        unsubscribe_url=unsub,
        original_subject=original_subject,
        referred_name=(variables_extra or {}).get("referred_name", ""),
    )
    variables.update(variables_extra or {})
    variables = {k: ("" if v is None else v) for k, v in variables.items()}
    subject = templates.render(template.subject, variables)
    body = templates.render(template.body_text, variables)
    if prospect is None:
        return None
    message = await sender.send_reply_message(
        session,
        prospect=prospect,
        company=company,
        contact=contact,
        step=step,
        locale=locale,
        subject=subject,
        body_text=body,
        in_reply_to=inbound.rfc_message_id,
        references=list(inbound.references or []) + [inbound.rfc_message_id],
        registry=registry,
        clock=clock,
        settings=settings,
    )
    if message.status == MessageStatus.QUEUED:
        await sender.send_one(
            session, message.id, registry=registry, clock=clock, settings=settings, worker_id="reply_handler"
        )
        await session.refresh(message)
    return message.id


async def _draft_for_human(
    session: AsyncSession,
    *,
    company: Company,
    contact: Contact | None,
    inbound: InboundMessage,
    reply: Reply,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
    locale: str,
) -> str:
    claims = load_claims()
    prompt_body, prompt_version = llm_client.load_prompt("draft_reply", "v1")
    context = {
        "language": locale,
        "facts": [claims.fact("intake", locale), claims.fact("comparison", locale), claims.fact("control", locale)],
        "booking_url": settings.booking_url,
        "features_url": settings.features_url,
    }
    import json

    user_prompt = (
        "Draft a reply for a human to review. Return JSON only.\n"
        "CONTEXT_JSON: " + json.dumps(context, ensure_ascii=False) + "\n"
        f"THEIR MESSAGE:\n<untrusted_data>\n{(inbound.cleaned_text or inbound.body_text)[:2500]}\n</untrusted_data>"
    )
    try:
        response = await llm_client.call_json(
            session,
            registry.llm,
            task="draft_reply",
            system=prompt_body,
            user=user_prompt,
            clock=clock,
            settings=settings,
            schema=DraftedReply,
            model_tier="fast",
            max_tokens=600,
            temperature=0.2,
            prompt_version=prompt_version,
        )
        return DraftedReply.model_validate(response.data).draft
    except Exception as exc:  # noqa: BLE001 - a missing draft must not block the task
        logger.warning("draft_failed", error=str(exc)[:160])
        return ""


async def handle(
    session: AsyncSession,
    reply: Reply,
    inbound: InboundMessage,
    *,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
) -> HandleResult:
    prospect = (
        await session.get(CampaignProspect, reply.campaign_prospect_id)
        if reply.campaign_prospect_id
        else None
    )
    if prospect is not None:
        company = await session.get(Company, prospect.company_id)
        contact = await session.get(Contact, prospect.contact_id) if prospect.contact_id else None
    else:
        company = await session.get(Company, inbound.company_id) if inbound.company_id else None
        contact = await session.get(Contact, inbound.contact_id) if inbound.contact_id else None
    if company is None:
        reply.status = ReplyStatus.NEEDS_HUMAN
        await handoff.create_task(
            session,
            task_type=TaskType.RESOLVE_ERROR,
            title="Unmatched inbound reply",
            brief=f"Could not match this inbound message to a company.\nSubject: {inbound.subject}",
            reply_id=reply.id,
            clock=clock,
        )
        return HandleResult(reply.classification, "task_created", notes=["unmatched_company"])

    campaign = await session.get(Campaign, prospect.campaign_id) if prospect else None
    from app.services.inbound import reply_language

    locale = reply_language(inbound.cleaned_text or inbound.body_text)
    email = contact_email(contact) or inbound.from_label
    phone = contact_phone(contact)
    result = HandleResult(reply.classification, "noop")
    klass = reply.classification
    allowed_auto = auto_classes(campaign)

    if klass in (ReplyClass.UNSUBSCRIBE, ReplyClass.COMPLAINT):
        reason = SuppressionReason.UNSUBSCRIBE if klass == ReplyClass.UNSUBSCRIBE else SuppressionReason.COMPLAINT
        await suppression.add_for_contact_reply(
            session, email=email, phone=phone, message_id=None, reason=reason
        )
        if prospect is not None:
            cancelled = await _cancel_pending(session, prospect.id, f"blocked_after_{klass}")
            result.notes.append(f"cancelled_pending={cancelled}")
        await _force_state(
            session, prospect, ProspectState.DO_NOT_CONTACT, clock=clock, reason=f"reply_{klass}"
        )
        reply.status = ReplyStatus.AUTO_HANDLED
        result.action = "suppressed_and_closed"
        if klass == ReplyClass.UNSUBSCRIBE and contact is not None and prospect is not None:
            confirm = await _send_auto_reply(
                session,
                prospect=prospect,
                company=company,
                contact=contact,
                inbound=inbound,
                step="reply_unsubscribe",
                locale=locale,
                registry=registry,
                clock=clock,
                settings=settings,
            )
            result.auto_message_id = confirm
        await audit.record(
            session,
            action="opt_out_processed",
            entity_type="reply",
            entity_id=reply.id,
            detail={"class": klass, "company_id": company.id},
        )
        return result

    if klass == ReplyClass.BOUNCE:
        kind = str(reply.extracted.get("bounce_kind", "unknown"))
        if kind in ("hard", "unknown"):
            await suppression.add_for_contact_reply(
                session, email=email, phone=None, message_id=None, reason=SuppressionReason.BOUNCE_HARD
            )
            await _force_state(session, prospect, ProspectState.BOUNCED, clock=clock, reason="hard_bounce")
            result.action = "suppressed_hard_bounce"
        else:
            if prospect is not None:
                await _force_state(session, prospect, ProspectState.PAUSED, clock=clock, reason="soft_bounce")
            result.action = "soft_bounce_retry_scheduled"
        reply.status = ReplyStatus.AUTO_HANDLED
        return result

    if klass in (ReplyClass.OUT_OF_OFFICE, ReplyClass.AUTO_REPLY):
        days = OOO_DEFAULT_DAYS
        return_date = str(reply.extracted.get("return_date", "") or "")
        if return_date:
            try:
                from datetime import date

                parsed = date.fromisoformat(return_date[:10])
                days = max(1, (parsed - clock.today()).days)
            except ValueError:
                days = OOO_DEFAULT_DAYS
        if prospect is not None:
            await _force_state(session, prospect, ProspectState.PAUSED, clock=clock, reason=klass)
            prospect.next_action_at = clock.now() + timedelta(days=days)
            prospect.replied_at = None  # an OOO is not a reply; keep the sequence alive
        reply.status = ReplyStatus.AUTO_HANDLED
        result.action = f"paused_for_{days}_days"
        return result

    if klass == ReplyClass.NOT_INTERESTED:
        await _force_state(
            session, prospect, ProspectState.NOT_INTERESTED, clock=clock, reason="not_interested"
        )
        await _force_state(session, prospect, ProspectState.CLOSED_LOST, clock=clock, reason="not_interested")
        reply.status = ReplyStatus.AUTO_HANDLED
        result.action = "closed_lost"
        if "not_interested" in allowed_auto and contact is not None and prospect is not None:
            result.auto_message_id = await _send_auto_reply(
                session,
                prospect=prospect,
                company=company,
                contact=contact,
                inbound=inbound,
                step="reply_unsubscribe",
                locale=locale,
                registry=registry,
                clock=clock,
                settings=settings,
            )
        return result

    if klass == ReplyClass.NOT_NOW:
        await _force_state(session, prospect, ProspectState.NOT_NOW, clock=clock, reason="not_now")
        await _force_state(session, prospect, ProspectState.SNOOZED, clock=clock, reason="not_now")
        if prospect is not None:
            prospect.snoozed_until = clock.now() + timedelta(days=SNOOZE_DAYS)
            prospect.next_action_at = prospect.snoozed_until
        reply.status = ReplyStatus.AUTO_HANDLED
        result.action = f"snoozed_{SNOOZE_DAYS}_days"
        return result

    if klass == ReplyClass.REFERRAL:
        referred_email = str(reply.extracted.get("referred_email", "") or "")
        referred_name = str(reply.extracted.get("referred_person", "") or "")
        new_contact = None
        if referred_email:
            new_contact = await create_contact(
                session,
                company_id=company.id,
                email=referred_email,
                full_name=referred_name or None,
                role_category="procurement",
                source_url=f"referral:{inbound.rfc_message_id}",
                confidence=0.85,
                consent_source="provided_by_reply",
            )
            if prospect is not None and campaign is not None:
                # One conversation per company per campaign is a hard invariant (unique index).
                # A referral therefore *re-points* the existing prospect at the better contact
                # instead of opening a second thread.
                existing = await session.execute(
                    select(CampaignProspect).where(
                        CampaignProspect.campaign_id == campaign.id,
                        CampaignProspect.company_id == company.id,
                    )
                )
                target = existing.scalars().first()
                if target is None:
                    target = CampaignProspect(
                        campaign_id=campaign.id,
                        company_id=company.id,
                        contact_id=new_contact.id,
                        state=ProspectState.QUEUED,
                        step_index=0,
                        next_action_at=clock.now(),
                        language=new_contact.preferred_language,
                        notes="referred_by_reply",
                    )
                    session.add(target)
                    await session.flush()
                    await state_machine.transition(
                        session,
                        target,
                        ProspectState.QUEUED,
                        clock=clock,
                        reason="referral_from_reply",
                    )
                else:
                    target.contact_id = new_contact.id
                    target.notes = ((target.notes or "") + " referred_by_reply").strip()
                    target.next_action_at = clock.now()
                    await session.flush()
        if contact is not None and prospect is not None:
            result.auto_message_id = await _send_auto_reply(
                session,
                prospect=prospect,
                company=company,
                contact=contact,
                inbound=inbound,
                step="reply_referral_thanks",
                locale=locale,
                registry=registry,
                clock=clock,
                settings=settings,
                variables_extra={"referred_name": referred_name or referred_email},
            )
        await _force_state(
            session, prospect, ProspectState.REFERRAL_FOLLOWED, clock=clock, reason="referral"
        )
        reply.status = ReplyStatus.AUTO_HANDLED
        result.action = "referral_contact_created" if new_contact else "referral_no_email"
        return result

    if klass == ReplyClass.WANTS_INFO:
        if "wants_info" in allowed_auto and contact is not None and prospect is not None:
            result.auto_message_id = await _send_auto_reply(
                session,
                prospect=prospect,
                company=company,
                contact=contact,
                inbound=inbound,
                step="reply_info",
                locale=locale,
                registry=registry,
                clock=clock,
                settings=settings,
            )
            reply.status = ReplyStatus.AUTO_HANDLED
            result.action = "info_sent"
        else:
            draft = await _draft_for_human(
                session,
                company=company,
                contact=contact,
                inbound=inbound,
                reply=reply,
                registry=registry,
                clock=clock,
                settings=settings,
                locale=locale,
            )
            task = await handoff.create_task(
                session,
                task_type=TaskType.REPLY_DRAFT,
                title=f"Reply draft: information request — {company.name}",
                brief=f"{company.name} asked for information.",
                company_id=company.id,
                contact_id=contact.id if contact else None,
                reply_id=reply.id,
                draft=draft,
                clock=clock,
                due_in_hours=4,
            )
            reply.status = ReplyStatus.NEEDS_HUMAN
            result.task_id = task.id
            result.action = "task_created"
        return result

    if klass in (ReplyClass.INTERESTED, ReplyClass.MEETING_PROPOSAL):
        if contact is not None:
            result.auto_message_id = await _send_auto_reply(
                session,
                prospect=prospect,
                company=company,
                contact=contact,
                inbound=inbound,
                step="reply_booking",
                locale=locale,
                registry=registry,
                clock=clock,
                settings=settings,
            )
        handoff_result = await handoff.handoff(
            session,
            company=company,
            contact=contact,
            prospect=prospect,
            reply=reply,
            inbound=inbound,
            registry=registry,
            clock=clock,
            settings=settings,
        )
        result.task_id = handoff_result["task_id"]
        result.demo_id = handoff_result["demo_id"]
        await _force_state(
            session, prospect, state_machine.state_for_reply(klass), clock=clock, reason=klass
        )
        await _force_state(session, prospect, ProspectState.HANDED_OFF, clock=clock, reason="handoff")
        reply.status = ReplyStatus.NEEDS_HUMAN
        result.action = "booked_link_sent_and_handed_off"
        result.notes.append(f"notified={handoff_result['notified']}")
        return result

    # question_pricing / question_technical / unclear → human only, never auto-sent
    draft = await _draft_for_human(
        session,
        company=company,
        contact=contact,
        inbound=inbound,
        reply=reply,
        registry=registry,
        clock=clock,
        settings=settings,
        locale=locale,
    )
    task = await handoff.create_task(
        session,
        task_type=TaskType.REPLY_DRAFT,
        title=f"Reply draft: {klass} — {company.name}",
        brief=(
            f"Classified as {klass} (confidence {reply.confidence:.2f}). "
            "Pricing and commitments are always human-handled."
        ),
        company_id=company.id,
        contact_id=contact.id if contact else None,
        reply_id=reply.id,
        draft=draft,
        clock=clock,
        due_in_hours=2 if klass == ReplyClass.QUESTION_PRICING else 8,
    )
    if prospect is not None:
        next_state = state_machine.state_for_reply(klass)
        await _force_state(session, prospect, next_state, clock=clock, reason=klass)
    reply.status = ReplyStatus.NEEDS_HUMAN
    result.task_id = task.id
    result.action = "task_created"
    return result


async def process_inbound(
    session: AsyncSession,
    inbound: InboundMessage,
    *,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
) -> dict[str, Any]:
    """Classify then execute the policy table for one inbound message."""
    reply, classification = await reply_classifier.classify(
        session, inbound, adapter=registry.llm, clock=clock, settings=settings
    )
    if reply is None:
        return {"classified": classification.reply_class, "handled": False}
    outcome = await handle(session, reply, inbound, registry=registry, clock=clock, settings=settings)
    await audit.emit_event(
        session,
        kind="reply_handled",
        entity_type="reply",
        entity_id=reply.id,
        payload={"class": classification.reply_class, "action": outcome.action},
    )
    return {
        "reply_id": reply.id,
        "classified": classification.reply_class,
        "confidence": classification.confidence,
        **outcome.as_dict(),
    }


async def thread_view(session: AsyncSession, prospect_id: str) -> list[dict[str, Any]]:
    return await recent_thread(session, prospect_id)


async def pending_review(session: AsyncSession, limit: int = 50) -> list[Reply]:
    rows = await session.execute(
        select(Reply).where(Reply.status == ReplyStatus.NEEDS_HUMAN).order_by(Reply.created_at.desc()).limit(limit)
    )
    return list(rows.scalars().all())


async def record_whatsapp_opt_out(
    session: AsyncSession, phone: str, clock: Clock, reason: str = "reply_stop"
) -> None:
    await consent.record_opt_out(session, phone=phone, reason=reason, clock=clock)
    await suppression.add(session, kind="phone", value=phone, reason=SuppressionReason.REPLY_STOP, created_by="system")
