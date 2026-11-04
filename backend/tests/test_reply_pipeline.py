"""Inbound → classify → policy: opt-out is absolute, OOO pauses, bounces suppress, humans get context."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.core.clock import FrozenClock
from app.db.models import CampaignProspect, OutreachMessage, Reply, Task
from app.services import inbound as inbound_service
from app.services import reply_handler, suppression, templates
from tests.conftest import make_campaign, make_company, make_contact, make_prospect


@pytest.fixture
async def thread(session: Any, settings: Any, clock: FrozenClock) -> dict[str, Any]:
    await templates.seed_templates(session)
    company = await make_company(session, language="ar")
    contact = await make_contact(session, company.id, email="procurement@delta.example", email_status="valid")
    campaign = await make_campaign(session, auto_reply_classes=["unsubscribe", "wants_info", "meeting_proposal", "referral"])
    prospect = await make_prospect(
        session, campaign, company, contact, state="step1_sent", step_index=0, last_step_sent_at=clock.now() - timedelta(days=1)
    )
    outbound = OutreachMessage(
        campaign_prospect_id=prospect.id,
        company_id=company.id,
        contact_id=contact.id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI لمصنعكم",
        body_text="…",
        to_domain="delta.example",
        status="sent",
        rfc_message_id="<out-1@mantiqoutreach.example>",
        idempotency_key="out-1",
        sent_at=clock.now() - timedelta(days=1),
    )
    session.add(outbound)
    await session.flush()
    return {"company": company, "contact": contact, "campaign": campaign, "prospect": prospect, "outbound": outbound}


async def reply_with(
    session: Any,
    settings: Any,
    clock: FrozenClock,
    thread: dict[str, Any],
    body: str,
    *,
    subject: str = "Re: ProcureAI",
    headers: dict[str, str] | None = None,
    from_email: str = "procurement@delta.example",
    script: str | None = None,
    extracted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if script:
        from app.adapters.registry import get_registry

        get_registry().llm.push(
            "classify_reply",
            {
                "class": script,
                "confidence": 0.93,
                "possible_opt_out": script == "unsubscribe",
                "language": "en",
                "extracted": extracted or {},
            },
        )
    raw = inbound_service.RawInbound(
        message_id=f"<in-{abs(hash(body))}@delta.example>",
        from_email=from_email,
        from_label="Delta Procurement",
        subject=subject,
        body_text=body,
        in_reply_to=thread["outbound"].rfc_message_id,
        references=[thread["outbound"].rfc_message_id],
        headers=headers or {},
        received_at=clock.now(),
    )
    inbound_row = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    outcome = await reply_handler.process_inbound(
        session, inbound_row, registry=__import__("app.adapters.registry", fromlist=["get_registry"]).get_registry(), clock=clock, settings=settings
    )
    return {"inbound": inbound_row, "outcome": outcome}


async def test_ingest_links_thread_and_pauses_sequence(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    raw = inbound_service.RawInbound(
        message_id="<in-1@delta.example>",
        from_email="procurement@delta.example",
        subject="Re: ProcureAI",
        body_text="شكرًا، مهتمين نعرف أكتر",
        in_reply_to=thread["outbound"].rfc_message_id,
        received_at=clock.now(),
    )
    row = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    assert row.company_id == thread["company"].id
    assert row.contact_id == thread["contact"].id
    assert row.campaign_prospect_id == thread["prospect"].id
    assert row.in_reply_to == thread["outbound"].rfc_message_id
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.replied_at is not None
    outbound = await session.get(OutreachMessage, thread["outbound"].id)
    assert outbound.status == "replied"


async def test_duplicate_inbound_is_idempotent(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    raw = inbound_service.RawInbound(
        message_id="<dup-1@delta.example>",
        from_email="procurement@delta.example",
        body_text="hello",
        in_reply_to=thread["outbound"].rfc_message_id,
        received_at=clock.now(),
    )
    first = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    second = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    assert first.id == second.id


@pytest.mark.parametrize(
    "body",
    [
        "إيقاف",
        "ابعتولي وقف الرسائل",
        "مش عايزين نستلم رسائل تاني، شيلوني",
        "Please unsubscribe me from this list",
        "STOP",
    ],
)
async def test_opt_out_suppresses_and_closes(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any], body: str
) -> None:
    result = await reply_with(session, settings, clock, thread, body)
    assert result["outcome"]["classified"] == "unsubscribe"
    assert await suppression.is_suppressed(session, email="procurement@delta.example") is not None
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "do_not_contact"
    reply = (
        await session.execute(
            Reply.__table__.select().where(Reply.inbound_message_id == result["inbound"].id)
        )
    ).first()
    assert reply is not None
    assert reply.status == "auto_handled"


async def test_opt_out_acknowledgement_is_sent_once(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any], registry: Any
) -> None:
    result = await reply_with(session, settings, clock, thread, "أوقفوا الرسائل من فضلكم")
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.db.models import OutreachMessage as OM

    msg = await session.get(OM, result["outcome"]["auto_message_id"])
    # dry_run records "would_send" (the adapter is never called); a real mode sends it once.
    assert msg.status in {"queued", "sent", "would_send"}, (msg.status, msg.blocked_reason)
    assert msg.blocked_reason is None
    assert msg.check_report["passed"] is True
    assert msg.in_reply_to  # threaded, 1:1
    total = await session.execute(
        sa_select(func.count()).select_from(OM).where(OM.step == "reply_unsubscribe")
    )
    assert int(total.scalar_one()) == 1  # exactly one confirmation, never a loop
    del registry


async def test_complaint_suppresses_without_reply(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any], registry: Any
) -> None:
    before = len(registry.email.sent)
    result = await reply_with(session, settings, clock, thread, "This is spam, I am reporting you to the authorities")
    assert result["outcome"]["classified"] in {"complaint", "unsubscribe"}
    assert len(registry.email.sent) == before  # never argue with a complaint
    assert await suppression.is_suppressed(session, email="procurement@delta.example") is not None


async def test_out_of_office_pauses_without_ending_sequence(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    result = await reply_with(session, settings, clock, thread, "I am out of the office until next Sunday.")
    assert result["outcome"]["classified"] == "out_of_office"
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "paused"
    assert prospect.replied_at is None
    assert prospect.next_action_at > clock.now()


async def test_auto_reply_pauses_sequence(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    result = await reply_with(
        session,
        settings,
        clock,
        thread,
        "Thank you for your email. This is an automated response.",
        headers={"auto-submitted": "auto-replied"},
    )
    assert result["outcome"]["classified"] in {"auto_reply", "out_of_office"}
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "paused"
    assert prospect.replied_at is None


async def test_hard_bounce_suppresses_and_marks_bounced(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    body = (
        "Delivery Status Notification\n"
        "Your message could not be delivered. The email account does not exist. 5.1.1 user unknown"
    )
    result = await reply_with(session, settings, clock, thread, body, from_email="mailer-daemon@delta.example")
    assert result["outcome"]["classified"] == "bounce"
    assert await suppression.is_suppressed(session, email="procurement@delta.example") is not None
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "bounced"


async def test_soft_bounce_pauses(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    body = "Delivery Status Notification: mailbox full, quota exceeded 4.2.2"
    result = await reply_with(session, settings, clock, thread, body, from_email="mailer-daemon@delta.example")
    assert result["outcome"]["classified"] == "bounce"
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "paused"
    assert await suppression.is_suppressed(session, email="procurement@delta.example") is None


async def test_not_interested_closes_lost(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    result = await reply_with(
        session,
        settings,
        clock,
        thread,
        "We are not interested, we already use another procurement tool.",
        script="not_interested",
    )
    assert result["outcome"]["classified"] == "not_interested"
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "closed_lost"


async def test_not_now_snoozes(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    result = await reply_with(
        session, settings, clock, thread, "Not now, please contact us again after the summer.", script="not_now"
    )
    assert result["outcome"]["classified"] == "not_now"
    prospect = await session.get(CampaignProspect, thread["prospect"].id)
    assert prospect.state == "snoozed"
    assert prospect.snoozed_until
    assert prospect.next_action_at


async def test_wants_info_gets_reply_or_review(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    result = await reply_with(
        session,
        settings,
        clock,
        thread,
        "Interested. Please send more details about pricing and how the approval workflow works.",
        script="wants_info",
        extracted={"questions": ["pricing", "approval workflow"]},
    )
    assert result["outcome"]["classified"] == "wants_info"
    assert result["outcome"]["action"] in {"info_sent", "auto_replied", "drafted_for_review", "reply_queued", "noop"}
    reply = (
        await session.execute(Reply.__table__.select().where(Reply.inbound_message_id == result["inbound"].id))
    ).first()
    assert reply is not None


async def test_meeting_proposal_creates_handoff_task_and_notifies(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any], registry: Any
) -> None:
    result = await reply_with(
        session,
        settings,
        clock,
        thread,
        "Can we book a call on Tuesday morning? Our CFO would like to see a demo.",
        script="meeting_proposal",
        extracted={"requested_times": ["Tuesday morning"]},
    )
    assert result["outcome"]["classified"] == "meeting_proposal"
    tasks = (await session.execute(Task.__table__.select())).all()
    assert tasks, "a human hand-off task must exist"
    assert registry.notify.alerts, "staff must be notified"


async def test_referral_creates_contact(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    result = await reply_with(
        session,
        settings,
        clock,
        thread,
        "Please talk to our supply chain manager Mona at mona.supply@delta.example",
        script="referral",
        extracted={"referred_person": "Mona", "referred_email": "mona.supply@delta.example"},
    )
    assert result["outcome"]["classified"] == "referral"
    from app.services import contacts as contact_service

    assert await contact_service.find_by_email(session, "mona.supply@delta.example") is not None


async def test_unmatched_inbound_creates_error_task(
    session: Any, settings: Any, clock: FrozenClock, registry: Any
) -> None:
    raw = inbound_service.RawInbound(
        message_id="<unknown-1@elsewhere.example>",
        from_email="stranger@elsewhere.example",
        subject="Interested",
        body_text="Who are you?",
        received_at=clock.now(),
    )
    row = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    outcome = await reply_handler.process_inbound(
        session, row, registry=registry, clock=clock, settings=settings
    )
    assert outcome["action"] in {"task_created", "noop", "drafted_for_review"}
    tasks = (await session.execute(Task.__table__.select())).all()
    assert tasks


async def test_thread_view_returns_conversation(
    session: Any, settings: Any, clock: FrozenClock, thread: dict[str, Any]
) -> None:
    await reply_with(session, settings, clock, thread, "مهتمين، ابعتوا تفاصيل")
    rows = await inbound_service.recent_thread(session, thread["prospect"].id)
    assert rows
    view = await reply_handler.thread_view(session, thread["prospect"].id)
    assert isinstance(view, list)
    assert view


async def test_language_detection_on_replies() -> None:
    assert inbound_service.reply_language("مهتمين جدًا، كلمونا") == "ar"
    assert inbound_service.reply_language("We are interested, call us") == "en"
