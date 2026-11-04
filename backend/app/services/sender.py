"""Sending: claim → re-run the gate → send → record. Exactly once, even with concurrent workers.

The claim is a conditional UPDATE (`status='queued' → 'sending'`). Only the worker whose UPDATE
affects a row proceeds, so two workers racing over the same due message cannot both send it.
Immediately before the adapter call the full Send Gate runs again (suppression is re-checked at
the last possible moment).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import EmailEvent, OutboundEmail
from app.adapters.email.mime import stable_message_id
from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import NotFound
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.db.models import (
    Campaign,
    CampaignProspect,
    Company,
    Contact,
    Mailbox,
    MessageEvent,
    OutreachMessage,
)
from app.db.models.enums import (
    MessageEventType,
    MessageStatus,
    ProspectState,
    SuppressionReason,
    TemplateStep,
)
from app.services import (
    audit,
    composer,
    send_gate,
    state_machine,
    suppression,
    templates,
)
from app.services import (
    mailboxes as mailbox_service,
)
from app.services.contacts import contact_email

logger = get_logger("sender")


@dataclass
class SendBatchResult:
    claimed: int = 0
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "blocked": self.blocked,
            "failed": self.failed,
            "skipped": self.skipped,
            "details": self.details,
        }


def text_to_html(body_text: str, unsubscribe_url: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body_text or "") if p.strip()]
    rendered = "\n".join(
        '<p style="margin:0 0 12px 0;line-height:1.6">'
        + html.escape(p).replace("\n", "<br/>")
        + "</p>"
        for p in paragraphs
    )
    footer = (
        f'<p style="margin-top:24px;font-size:12px;color:#666">'
        f'<a href="{html.escape(unsubscribe_url)}">Unsubscribe / إلغاء الاشتراك</a></p>'
    )
    return (
        '<html><body dir="auto" style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#111">'
        f"{rendered}{footer}</body></html>"
    )


async def _claim(session: AsyncSession, message_id: str, worker_id: str) -> bool:
    result = await session.execute(
        update(OutreachMessage)
        .where(
            OutreachMessage.id == message_id,
            OutreachMessage.status == MessageStatus.QUEUED,
            OutreachMessage.direction == "outbound",
        )
        .values(status=MessageStatus.SENDING, locked_by=worker_id)
    )
    await session.flush()
    return bool(cast(CursorResult[Any], result).rowcount)


async def _register_event(
    session: AsyncSession, message: OutreachMessage, event_type: str, payload: dict[str, Any], clock: Clock
) -> None:
    session.add(
        MessageEvent(message_id=message.id, type=event_type, payload=payload, occurred_at=clock.now())
    )
    await session.flush()


async def _reschedule(session: AsyncSession, message: OutreachMessage, prospect: CampaignProspect | None, clock: Clock, hours: int = 1) -> None:
    message.status = MessageStatus.QUEUED
    message.scheduled_for = clock.now() + timedelta(hours=hours)
    if prospect is not None:
        prospect.next_action_at = message.scheduled_for
    await session.flush()


async def send_one(
    session: AsyncSession,
    message_id: str,
    *,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
    worker_id: str = "worker",
) -> dict[str, Any]:
    message = await session.get(OutreachMessage, message_id)
    if message is None:
        raise NotFound("message not found")
    claimed = await _claim(session, message_id, worker_id)
    if not claimed:
        return {"message_id": message_id, "result": "not_claimed"}
    await session.refresh(message)

    company = await session.get(Company, message.company_id)
    contact = await session.get(Contact, message.contact_id) if message.contact_id else None
    campaign = None
    prospect = None
    if message.campaign_prospect_id:
        prospect = await session.get(CampaignProspect, message.campaign_prospect_id)
        if prospect is not None:
            campaign = await session.get(Campaign, prospect.campaign_id)
    mailbox = await session.get(Mailbox, message.mailbox_id) if message.mailbox_id else None
    if company is None:
        message.status = MessageStatus.FAILED
        message.blocked_reason = "company_missing"
        await session.flush()
        return {"message_id": message_id, "result": "failed", "reason": "company_missing"}

    unsubscribe_url = composer.unsubscribe_url_for(
        contact.id if contact else "unknown",
        campaign.id if campaign else None,
        settings.public_base_url,
        settings.unsubscribe_token_ttl_days,
    )
    ctx = await send_gate.build_context(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        prospect=prospect,
        mailbox=mailbox,
        message=message,
        channel=message.channel,
        step=message.step,
        locale=message.locale,
        subject=message.subject,
        body_text=message.body_text,
        unsubscribe_url=unsubscribe_url,
        settings=settings,
        clock=clock,
        claims_report=message.claim_report or None,
    )
    report = await send_gate.evaluate(session, ctx, clock, settings)
    message.check_report = report.as_dict()
    if not report.passed:
        message.status = MessageStatus.BLOCKED
        message.blocked_reason = (report.blocked_reason or "blocked")[:190]
        await session.flush()
        metrics.incr("outreach_blocked_total", reason=(report.blocked_reason or "unknown").split(":")[0])
        if prospect is not None and report.permanent:
            if prospect.state in (
                ProspectState.COMPOSING,
                ProspectState.QUEUED,
                ProspectState.STEP1_SENT,
                ProspectState.FOLLOWUP_1_SENT,
                ProspectState.FOLLOWUP_2_SENT,
            ):
                await state_machine.transition(
                    session,
                    prospect,
                    ProspectState.BLOCKED_BY_GATE,
                    clock=clock,
                    reason=message.blocked_reason or "blocked",
                )
        elif report.blocked_reason and report.blocked_reason.startswith("send_window") and prospect is not None:
            next_start = None
            for check in report.failures:
                if check.rule == "send_window":
                    next_start = check.detail.get("next_start")
            if next_start:
                from datetime import datetime

                try:
                    prospect.next_action_at = datetime.fromisoformat(str(next_start))
                except ValueError:
                    prospect.next_action_at = clock.now() + timedelta(hours=6)
        await audit.record(
            session,
            action="send_blocked",
            entity_type="outreach_message",
            entity_id=message.id,
            detail={"reason": message.blocked_reason, "rules": [c.rule for c in report.failures]},
        )
        return {"message_id": message_id, "result": "blocked", "reason": message.blocked_reason}

    if ctx.mode == "dry_run":
        message.status = MessageStatus.WOULD_SEND
        await session.flush()
        return {"message_id": message_id, "result": "would_send"}

    body_html = text_to_html(message.body_text, unsubscribe_url)
    outbound = OutboundEmail(
        to_email=contact_email(contact),
        from_email=(mailbox.from_email if mailbox else settings.sender_email),
        from_name=(mailbox.from_name if mailbox else settings.sender_name),
        subject=message.subject,
        body_text=message.body_text,
        body_html=body_html,
        reply_to=(mailbox.reply_to if mailbox else settings.sender_email) or settings.sender_email,
        message_id=stable_message_id(
            OutboundEmail(
                to_email=contact_email(contact),
                from_email=(mailbox.from_email if mailbox else settings.sender_email),
                from_name="",
                subject=message.subject,
                body_text="",
            ),
            salt=message.idempotency_key or "",
        ),
        in_reply_to=message.in_reply_to,
        references=list(message.references or []),
        unsubscribe_url=unsubscribe_url,
        headers={"X-Mantiq-Campaign": message.campaign_prospect_id or "none"},
    )
    result = await registry.email.send(outbound)
    if result.ok:
        message.status = MessageStatus.SENT
        message.sent_at = clock.now()
        message.provider = result.provider
        message.provider_message_id = result.provider_message_id
        message.rfc_message_id = result.rfc_message_id
        message.sender_email = outbound.from_email
        message.blocked_reason = None
        if mailbox is not None:
            mailbox.last_sent_at = clock.now()
        await _register_event(session, message, MessageEventType.SENT, {"provider": result.provider}, clock)
        if prospect is not None:
            prospect.last_message_id = message.id
            prospect.last_step_sent_at = clock.now()
            prospect.step_index = prospect.step_index + 1
            prospect.thread_id = prospect.thread_id or message.rfc_message_id or message.id
            new_state = state_machine.state_after_send(message.step)
            if state_machine.is_legal(prospect.state, new_state):
                await state_machine.transition(
                    session, prospect, new_state, clock=clock, reason=f"sent:{message.step}"
                )
            gaps = (campaign.followup_gaps_days if campaign else [3, 7]) or [3]
            followups_used = prospect.step_index - 1
            if campaign is not None and prospect.step_index <= campaign.max_followups and message.step != TemplateStep.BREAKUP:
                gap = gaps[min(max(0, followups_used - 1), len(gaps) - 1)]
                prospect.next_action_at = clock.now() + timedelta(days=gap)
            else:
                prospect.next_action_at = None
        metrics.incr("outreach_sent_total", channel=message.channel)
        await audit.record(
            session,
            action="message_sent",
            entity_type="outreach_message",
            entity_id=message.id,
            detail={"provider": result.provider, "step": message.step, "locale": message.locale},
        )
        await audit.emit_event(
            session,
            kind="message_sent",
            entity_type="outreach_message",
            entity_id=message.id,
            payload={"company_id": message.company_id, "step": message.step},
        )
        await session.flush()
        return {"message_id": message_id, "result": "sent", "provider": result.provider}

    # failure
    message.status = MessageStatus.FAILED if result.permanent else MessageStatus.QUEUED
    message.blocked_reason = (result.error or "send_failed")[:190]
    if not result.permanent:
        await _reschedule(session, message, prospect, clock, hours=1)
    else:
        email = contact_email(contact)
        if email:
            await suppression.add(
                session,
                kind="email",
                value=email,
                reason=SuppressionReason.BOUNCE_HARD,
                created_by="sender",
                source_message_id=message.id,
                note=(result.error or "")[:200],
            )
        if prospect is not None and state_machine.is_legal(prospect.state, ProspectState.BOUNCED):
            await state_machine.transition(
                session, prospect, ProspectState.BOUNCED, clock=clock, reason="permanent_send_failure"
            )
    await _register_event(
        session, message, MessageEventType.BOUNCE_HARD if result.permanent else MessageEventType.BOUNCE_SOFT, {"error": result.error}, clock
    )
    metrics.incr("outreach_send_failed_total", permanent=str(result.permanent).lower())
    await session.flush()
    return {"message_id": message_id, "result": "failed", "permanent": result.permanent, "error": result.error}


async def due_message_ids(session: AsyncSession, clock: Clock, limit: int = 25) -> list[str]:
    result = await session.execute(
        select(OutreachMessage.id)
        .where(
            OutreachMessage.status == MessageStatus.QUEUED,
            OutreachMessage.direction == "outbound",
            OutreachMessage.scheduled_for.is_not(None),
            OutreachMessage.scheduled_for <= clock.now(),
        )
        .order_by(OutreachMessage.scheduled_for.asc())
        .limit(limit)
    )
    return [str(row) for row in result.scalars().all()]


async def send_due(
    session: AsyncSession,
    *,
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
    limit: int = 25,
    worker_id: str = "worker",
) -> SendBatchResult:
    batch = SendBatchResult()
    ids = await due_message_ids(session, clock, limit)
    for message_id in ids:
        batch.claimed += 1
        outcome = await send_one(
            session, message_id, registry=registry, clock=clock, settings=settings, worker_id=worker_id
        )
        result = outcome.get("result")
        if result == "sent":
            batch.sent += 1
        elif result == "blocked":
            batch.blocked += 1
        elif result == "failed":
            batch.failed += 1
        else:
            batch.skipped += 1
        batch.details.append(outcome)
    return batch


async def process_email_event(
    session: AsyncSession, event: EmailEvent, *, registry: AdapterRegistry, clock: Clock
) -> dict[str, Any]:
    """Provider webhook / DSN event → message + prospect + suppression side-effects."""
    message = None
    if event.message_id:
        found = await session.execute(
            select(OutreachMessage).where(
                (OutreachMessage.rfc_message_id == event.message_id)
                | (OutreachMessage.provider_message_id == event.message_id)
            )
        )
        message = found.scalars().first()
    if message is None and event.recipient:
        from app.core.crypto import blind_index
        from app.core.text import email_match_key

        digest = blind_index(email_match_key(event.recipient), purpose="email")
        found = await session.execute(
            select(OutreachMessage)
            .where(OutreachMessage.to_email_hash == digest)
            .order_by(OutreachMessage.created_at.desc())
            .limit(1)
        )
        message = found.scalars().first()
    if message is None:
        return {"result": "unmatched", "event": event.type}

    recipient = event.recipient
    if not recipient and message.contact_id:
        contact_row = await session.get(Contact, message.contact_id)
        recipient = contact_email(contact_row) or None
    payload = {"reason": event.reason, **(event.meta or {})}
    if event.type == "delivered":
        if message.status == MessageStatus.SENT:
            message.status = MessageStatus.DELIVERED
        await _register_event(session, message, MessageEventType.DELIVERED, payload, clock)
        return {"result": "delivered", "message_id": message.id}
    if event.type == "bounce_hard":
        message.status = MessageStatus.BOUNCED_HARD
        await _register_event(session, message, MessageEventType.BOUNCE_HARD, payload, clock)
        if recipient:
            await suppression.add(
                session,
                kind="email",
                value=recipient,
                reason=SuppressionReason.BOUNCE_HARD,
                created_by="provider_webhook",
                source_message_id=message.id,
            )
        prospect = (
            await session.get(CampaignProspect, message.campaign_prospect_id)
            if message.campaign_prospect_id
            else None
        )
        if prospect is not None and state_machine.is_legal(prospect.state, ProspectState.BOUNCED):
            await state_machine.transition(
                session, prospect, ProspectState.BOUNCED, clock=clock, reason="hard_bounce"
            )
        return {"result": "bounce_hard", "message_id": message.id}
    if event.type == "bounce_soft":
        message.status = MessageStatus.BOUNCED_SOFT
        await _register_event(session, message, MessageEventType.BOUNCE_SOFT, payload, clock)
        return {"result": "bounce_soft", "message_id": message.id}
    if event.type == "complaint":
        message.status = MessageStatus.BOUNCED_HARD
        await _register_event(session, message, MessageEventType.COMPLAINT, payload, clock)
        if recipient:
            await suppression.add_for_contact_reply(
                session,
                email=recipient,
                phone=None,
                message_id=message.id,
                reason=SuppressionReason.COMPLAINT,
            )
        prospect = (
            await session.get(CampaignProspect, message.campaign_prospect_id)
            if message.campaign_prospect_id
            else None
        )
        if prospect is not None and state_machine.is_legal(prospect.state, ProspectState.DO_NOT_CONTACT):
            await state_machine.transition(
                session, prospect, ProspectState.DO_NOT_CONTACT, clock=clock, reason="complaint"
            )
        return {"result": "complaint", "message_id": message.id}
    if event.type == "open":
        await _register_event(session, message, MessageEventType.OPEN, payload, clock)
        return {"result": "open", "message_id": message.id}
    if event.type == "click":
        await _register_event(session, message, MessageEventType.CLICK, payload, clock)
        return {"result": "click", "message_id": message.id}
    return {"result": "ignored", "event": event.type}


async def send_reply_message(
    session: AsyncSession,
    *,
    prospect: CampaignProspect,
    company: Company,
    contact: Contact,
    step: str,
    locale: str,
    subject: str,
    body_text: str,
    in_reply_to: str | None,
    references: list[str],
    registry: AdapterRegistry,
    clock: Clock,
    settings: Settings,
) -> OutreachMessage:
    """Reply-path send (auto-handled classes). Still passes the full gate and claim check."""
    campaign = await session.get(Campaign, prospect.campaign_id)
    mailbox = await mailbox_service.pick_mailbox(
        session, pool_id=campaign.mailbox_pool_id if campaign else None, clock=clock
    )
    from app.services import claim_check as claim_service

    claims = claim_service.load_claims()
    issues = claim_service.run_deterministic(
        body_text,
        subject=subject,
        allowed_hosts=claim_service.default_allowed_hosts(settings),
        approved_phrases=[p for p in (settings.poc_offer_text_ar, settings.poc_offer_text_en) if p],
        claims=claims,
    )
    claim_result = claim_service.ClaimCheckResult(
        passed=not issues, issues=issues, reason="" if not issues else "deterministic_claims"
    )
    unsub = composer.unsubscribe_url_for(
        contact.id, campaign.id if campaign else None, settings.public_base_url, settings.unsubscribe_token_ttl_days
    )
    message = OutreachMessage(
        campaign_prospect_id=prospect.id,
        company_id=company.id,
        contact_id=contact.id,
        channel="email",
        direction="outbound",
        step=step,
        locale=locale,
        subject=subject,
        body_text=body_text,
        status=MessageStatus.DRAFT,
        template_code=step,
        in_reply_to=in_reply_to,
        references=references,
        idempotency_key=send_gate.idempotency_key(prospect.id, step, "email"),
        to_email_hash=contact.email_hash,
        to_domain=contact_email(contact).partition("@")[2] or None,
        sender_email=settings.sender_email,
        claim_report=claim_result.as_report(),
        word_count=len(body_text.split()),
    )
    session.add(message)
    await session.flush()
    ctx = await send_gate.build_context(
        session,
        company=company,
        contact=contact,
        campaign=campaign,
        prospect=prospect,
        mailbox=mailbox,
        message=message,
        channel="email",
        step=step,
        locale=locale,
        subject=subject,
        body_text=body_text,
        unsubscribe_url=unsub,
        settings=settings,
        clock=clock,
        claims_report=claim_result.as_report(),
        claims=claims,
    )
    report = await send_gate.evaluate(session, ctx, clock, settings)
    message.check_report = report.as_dict()
    if not report.passed or ctx.mode == "dry_run":
        message.status = MessageStatus.BLOCKED if not report.passed else MessageStatus.WOULD_SEND
        message.blocked_reason = report.blocked_reason
        await session.flush()
        return message
    message.status = MessageStatus.QUEUED
    message.scheduled_for = clock.now()
    await session.flush()
    return message


async def retry_soft_bounces(session: AsyncSession, clock: Clock) -> int:
    """Soft bounces get exactly one retry after 24h, as the policy table specifies."""
    result = await session.execute(
        select(OutreachMessage).where(
            OutreachMessage.status == MessageStatus.BOUNCED_SOFT,
            OutreachMessage.updated_at <= clock.now() - timedelta(hours=24),
        )
    )
    retried = 0
    for message in result.scalars().all():
        events = await session.execute(
            select(MessageEvent).where(
                MessageEvent.message_id == message.id, MessageEvent.type == MessageEventType.BOUNCE_SOFT
            )
        )
        attempts = len(list(events.scalars().all()))
        if attempts >= 2:
            continue
        message.status = MessageStatus.QUEUED
        message.scheduled_for = clock.now()
        retried += 1
    await session.flush()
    return retried


async def rendering_health(session: AsyncSession) -> dict[str, Any]:
    rows = await templates.list_templates(session)
    return {"templates": len(rows), "active": len([r for r in rows if r.is_active])}
