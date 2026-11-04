"""Service-level pipeline: plan → compose → send, plus breakers, budgets, reports, discovery."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.core.clock import FrozenClock
from app.core.errors import BudgetExceeded, ValidationFailed
from app.db.models import CampaignProspect, Contact, OutreachMessage
from app.services import (
    budgets,
    circuit_breakers,
    deliverability,
    enrichment,
    export_erase,
    import_csv,
    mailboxes,
    qualification,
    reports,
    sender,
    sequencer,
    settings_service,
    state_machine,
    templates,
    verification,
)
from tests.conftest import make_campaign, make_company, make_contact, make_mailbox, make_prospect


@pytest.fixture
async def ready(session: Any, settings: Any, clock: FrozenClock) -> dict[str, Any]:
    await templates.seed_templates(session)
    company = await make_company(session, domain="pipeline.example", website="https://pipeline.example")
    contact = await make_contact(session, company.id, email="buyer@pipeline.example", email_status="valid")
    campaign = await make_campaign(
        session,
        name="Pipeline",
        send_windows={"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"},
        review_first_n=0,
    )
    mailbox = await make_mailbox(session, from_email="outreach@mantiqoutreach.example")
    return {"company": company, "contact": contact, "campaign": campaign, "mailbox": mailbox}


async def test_plan_sequences_creates_prospects(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any]
) -> None:
    result = await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    assert result.created == 1, result.reasons
    prospect = (
        await session.execute(
            CampaignProspect.__table__.select().where(CampaignProspect.company_id == ready["company"].id)
        )
    ).first()
    assert prospect is not None
    assert prospect.state == "queued"

    again = await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    assert again.created == 0  # idempotent


async def test_plan_skips_low_icp(session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any]) -> None:
    ready["company"].icp_score = 5
    ready["campaign"].min_icp_score = 60
    await session.flush()
    result = await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    assert result.created == 0
    assert any("icp" in reason.lower() for reason in result.reasons) or result.skipped >= 1


async def test_plan_skips_suppressed_company(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any]
) -> None:
    from app.services import suppression

    await suppression.add(session, kind="domain", value="pipeline.example", reason="manual")
    result = await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    assert result.created == 0


async def test_compose_due_queues_gated_message(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    batch = await sequencer.compose_due(session, registry=registry, clock=clock, settings=settings)
    assert batch.composed == 1, batch.notes
    assert batch.queued == 1, batch.notes  # dry_run queues as WOULD_SEND
    message = (
        await session.execute(
            OutreachMessage.__table__.select().where(OutreachMessage.direction == "outbound")
        )
    ).first()
    assert message is not None
    assert message.status in {"queued", "would_send"}  # dry_run => would_send, no adapter call
    assert message.check_report["passed"] is True
    assert message.idempotency_key


async def test_compose_routes_to_review_when_configured(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    ready["campaign"].review_first_n = 1
    await session.flush()
    await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    batch = await sequencer.compose_due(session, registry=registry, clock=clock, settings=settings)
    assert batch.review == 1
    assert batch.queued == 0


async def test_send_due_is_exactly_once(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    # sandbox with the recipient on the allow-list is the only non-dry mode that really sends
    await settings_service.set_mode(session, "sandbox", actor="test")
    sandboxed = settings.model_copy(update={"sandbox_allowlist": "buyer@pipeline.example"})
    await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=sandboxed)
    await sequencer.compose_due(session, registry=registry, clock=clock, settings=sandboxed)
    clock.advance(seconds=5)  # composed messages are scheduled ~1s ahead
    first = await sender.send_due(session, registry=registry, clock=clock, settings=sandboxed)
    assert first.claimed == 1
    second = await sender.send_due(session, registry=registry, clock=clock, settings=sandboxed)
    assert second.claimed == 0
    message = (
        await session.execute(
            OutreachMessage.__table__.select().where(OutreachMessage.direction == "outbound")
        )
    ).first()
    assert message is not None
    assert message.status in {"sent", "would_send"}
    assert registry.email.sent or message.status == "would_send"


async def test_sandbox_mode_sends_only_to_allowlist(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    await settings_service.set_mode(session, "sandbox", actor="test")
    await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    batch = await sequencer.compose_due(session, registry=registry, clock=clock, settings=settings)
    # buyer@pipeline.example is not on the allow-list, so the gate blocks it
    assert batch.blocked == 1 or batch.review == 0
    message = (
        await session.execute(
            OutreachMessage.__table__.select().where(OutreachMessage.direction == "outbound")
        )
    ).first()
    assert message is not None
    assert message.status in {"blocked", "needs_review"}
    if message.status == "blocked":
        assert "sandbox" in (message.blocked_reason or "")


async def test_kill_switch_stops_compose(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    await settings_service.set_kill_switch(session, True, actor="test", reason="stop")
    await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    batch = await sequencer.compose_due(session, registry=registry, clock=clock, settings=settings)
    assert batch.queued == 0


async def test_bounce_processing_marks_and_suppresses(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    from app.adapters.base import EmailEvent
    from app.services import suppression

    message = OutreachMessage(
        company_id=ready["company"].id,
        contact_id=ready["contact"].id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="body",
        status="sent",
        provider_message_id="prov-bounce-1",
        idempotency_key="bounce-1",
        sent_at=clock.now() - timedelta(minutes=5),
        campaign_prospect_id=(
            await make_prospect(session, ready["campaign"], ready["company"], ready["contact"], state="step1_sent")
        ).id,
    )
    session.add(message)
    await session.flush()
    event = EmailEvent(type="bounce_hard", message_id="prov-bounce-1")
    outcome = await sender.process_email_event(session, event, registry=registry, clock=clock)
    assert outcome["result"] == "bounce_hard"
    prospect = await session.get(CampaignProspect, message.campaign_prospect_id)
    assert prospect.state == "bounced"
    assert await suppression.is_suppressed(session, email="buyer@pipeline.example") is not None


async def test_complaint_is_recorded_and_suppressed(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    from app.adapters.base import EmailEvent
    from app.services import suppression

    message = OutreachMessage(
        company_id=ready["company"].id,
        contact_id=ready["contact"].id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="body",
        status="sent",
        provider_message_id="prov-1",
        idempotency_key="complaint-1",
        sent_at=clock.now() - timedelta(hours=1),
    )
    session.add(message)
    await session.flush()
    outcome = await sender.process_email_event(
        session, EmailEvent(type="complaint", message_id="prov-1"), registry=registry, clock=clock
    )
    assert outcome["result"] == "complaint"
    # the recipient is derived from the contact when the provider omits it
    assert await suppression.is_suppressed(session, email="buyer@pipeline.example") is not None
    verdict = await circuit_breakers.evaluate(session, registry=registry, clock=clock, settings=settings)
    assert verdict.rates["global"]["complaints"] >= 1


async def test_complaint_rate_trips_the_global_breaker(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    from app.adapters.base import EmailEvent

    for index in range(30):
        session.add(
            OutreachMessage(
                company_id=ready["company"].id,
                channel="email",
                direction="outbound",
                step="initial",
                subject="s",
                body_text="b",
                status="sent",
                sent_at=clock.now() - timedelta(hours=2),
                idempotency_key=f"bulk-{index}",
            )
        )
    session.add(
        OutreachMessage(
            company_id=ready["company"].id,
            channel="email",
            direction="outbound",
            step="initial",
            subject="s",
            body_text="b",
            status="sent",
            provider_message_id="prov-c1",
            idempotency_key="bulk-complaint",
            sent_at=clock.now() - timedelta(hours=1),
        )
    )
    await session.flush()
    await sender.process_email_event(
        session, EmailEvent(type="complaint", message_id="prov-c1"), registry=registry, clock=clock
    )
    verdict = await circuit_breakers.evaluate(session, registry=registry, clock=clock, settings=settings)
    assert verdict.tripped, verdict.as_dict()
    assert await settings_service.global_stop(session) is not None


async def test_mailbox_warmup_and_capacity(session: Any, clock: FrozenClock, settings: Any) -> None:
    mailbox = await make_mailbox(session, from_email="warm@mantiqoutreach.example", daily_cap=40)
    mailbox.created_at = clock.now() - timedelta(days=14)
    mailbox.warmup_stage = 1
    await session.flush()
    capacity = await mailboxes.capacity(session, mailbox, clock)
    assert capacity.daily_cap == 40
    assert capacity.warmup_cap <= 40
    assert capacity.remaining >= 0
    assert "@" in capacity.from_email  # masked but present
    advanced = await mailboxes.advance_warmup(session, clock)
    assert isinstance(advanced, list)
    picked = await mailboxes.pick_mailbox(session, pool_id=None, clock=clock)
    assert picked is not None


async def test_budgets_enforce_daily_limits(session: Any, settings: Any, clock: FrozenClock) -> None:
    await budgets.record_api_cost(session, provider="search", cost_usd=1.5, clock=clock)
    spend = await budgets.spent_today(session, clock)
    assert spend.total >= 1.5
    await settings_service.set_daily_budgets(session, 5.0, 1.0, actor="test")
    with pytest.raises(BudgetExceeded):
        await budgets.check(session, clock, settings)
    remaining = await budgets.remaining(session, clock, settings)
    assert remaining["api_remaining_usd"] == 0.0
    assert remaining["llm_remaining_usd"] == 5.0


async def test_state_machine_rules(session: Any, settings: Any, clock: FrozenClock) -> None:
    company = await make_company(session, domain="fsm.example")
    contact = await make_contact(session, company.id, email="x@fsm.example")
    campaign = await make_campaign(session, name="FSM")
    prospect = await make_prospect(session, campaign, company, contact)
    assert state_machine.is_legal("queued", "composing")
    assert not state_machine.is_legal("queued", "step1_sent")  # must pass through composing
    assert not state_machine.is_legal("do_not_contact", "step1_sent")
    assert state_machine.is_legal("queued", "do_not_contact")  # opt-out always reachable
    with pytest.raises(ValidationFailed):
        await state_machine.transition(session, prospect, "step1_sent", clock=clock, reason="test")
    await state_machine.transition(session, prospect, "do_not_contact", clock=clock, reason="test")
    assert prospect.state == "do_not_contact"
    with pytest.raises(ValidationFailed):
        await state_machine.transition(session, prospect, "queued", clock=clock, reason="test")


async def test_reports_funnel_and_kpis(
    session: Any, settings: Any, clock: FrozenClock, ready: dict[str, Any], registry: Any
) -> None:
    await sequencer.plan_sequences(session, campaign=ready["campaign"], clock=clock, settings=settings)
    await sequencer.compose_due(session, registry=registry, clock=clock, settings=settings)
    funnel = await reports.funnel(session)
    assert isinstance(funnel, list)
    assert funnel
    kpis = await reports.kpis(session, clock, settings)
    assert "sent" in kpis
    csv_text = reports.to_csv(funnel)
    assert isinstance(csv_text, str)
    assert "stage" in csv_text.lower()
    daily = await reports.daily_report(session, clock, settings)
    assert "kpis" in daily


async def test_deliverability_doctor_reports_checks(
    session: Any, settings: Any, clock: FrozenClock, registry: Any
) -> None:
    report = await deliverability.run_doctor(session, settings=settings, registry=registry, clock=clock)
    payload = report.as_dict()
    assert payload["checks"]
    names = {c["name"] for c in payload["checks"]}
    assert {"spf", "dkim", "dmarc", "unsubscribe_url", "kill_switch_off"} <= names
    assert payload["ok"] in {True, False}
    assert any(c["name"] == "dkim" and not c["ok"] for c in payload["checks"])  # no key in the sandbox


async def test_qualification_scoring(session: Any, settings: Any, clock: FrozenClock) -> None:
    company = await make_company(
        session,
        domain="qual.example",
        website="https://qual.example",
        industry="packaging_printing",
        status="enriched",
        icp_score=0,
    )
    await make_contact(session, company.id, email="procurement@qual.example", email_status="valid")
    result = await qualification.qualify_company(session, company=company, clock=clock, settings=settings)
    assert 0 <= result.score <= 100
    assert company.icp_score == result.score or company.status in {"excluded", "qualified"}


async def test_import_csv_preview_and_commit(session: Any, clock: FrozenClock) -> None:
    csv_text = "company_name,website,email,city\nAlpha Packs,https://alpha.example,buy@alpha.example,Cairo\n"
    preview = import_csv.preview(csv_text)
    preview = preview.as_dict() if hasattr(preview, "as_dict") else preview
    assert preview["total_rows"] == 1
    report = await import_csv.commit_import(
        session, content=csv_text, mapping=None, filename="t.csv", actor="test", clock=clock
    )
    assert report["imported"] == 1
    assert report["contacts_created"] == 1


async def test_export_and_erase(session: Any, clock: FrozenClock, settings: Any) -> None:
    company = await make_company(session, domain="erase.example")
    contact = await make_contact(session, company.id, email="erase@erase.example", email_status="valid")
    exported = await export_erase.export_company(session, company.id)
    assert exported["company"]["name"] == "Delta Packaging"
    assert "erase@erase.example" not in str(exported)  # masked in exports
    erased = await export_erase.erase_contact(session, contact.id, actor="test", clock=clock)
    assert erased["erased"] is True
    gone = await session.get(Contact, contact.id)
    assert gone is None or gone.email_enc is None


async def test_enrichment_and_verification(session: Any, settings: Any, clock: FrozenClock) -> None:
    company = await make_company(session, domain="enrich.example", website=None)
    result = await enrichment.enrich_company(
        session, company=company, crawler=None, clock=clock, settings=settings
    )
    assert hasattr(result, "pages")
    await make_contact(session, company.id, email="verify@enrich.example")
    from app.adapters.verify.local import LocalVerifyAdapter

    summary = await verification.verify_company_contacts(session, company.id, LocalVerifyAdapter(), clock)
    assert summary.as_dict()["checked"] >= 1
