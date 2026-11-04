"""The send gate: every rule, positive and negative, against real rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.clock import FrozenClock
from app.db.models import OutreachMessage
from app.db.models.enums import TemplateStep
from app.services import consent, send_gate, settings_service, suppression
from tests.conftest import make_campaign, make_company, make_contact, make_mailbox, make_prospect

GREETING = "مرحبًا،"


def good_body(settings: Any, unsubscribe_url: str, locale: str = "ar") -> str:
    if locale == "ar":
        return (
            "مرحبًا،\n\n"
            f"أنا {settings.sender_name} من Mantiq Technologies، بخصوص مشترياتكم: ProcureAI تجمع طلبات الشراء "
            "وتقارن عروض الموردين وتوثّق الاعتمادات.\n\n"
            "لو تحبوا نعمل عرض توضيحي قصير على بياناتكم، الرد يكفي.\n"
            f"لإيقاف الرسائل اضغطوا: {unsubscribe_url} (إيقاف)\n\n"
            f"Mantiq Technologies، {settings.company_address}، {settings.company_phone}"
        )
    return (
        "Hello,\n\n"
        f"I am {settings.sender_name} from Mantiq Technologies, following up about procurement: ProcureAI takes purchase "
        "requests, compares supplier quotes and documents approvals.\n\n"
        "If useful we can run a short demo on your data, just reply.\n"
        f"To stop these emails: {unsubscribe_url} (stop)\n\n"
        f"Mantiq Technologies, {settings.company_address}, {settings.company_phone}"
    )


@pytest.fixture
async def gate_setup(session: Any, settings: Any, clock: FrozenClock) -> dict[str, Any]:
    company = await make_company(session)
    contact = await make_contact(session, company.id, email_status="valid")
    campaign = await make_campaign(session)
    mailbox = await make_mailbox(session, from_email="sender-a@mantiqoutreach.example")
    prospect = await make_prospect(session, campaign, company, contact)
    return {
        "company": company,
        "contact": contact,
        "campaign": campaign,
        "mailbox": mailbox,
        "prospect": prospect,
    }


async def build(
    session: Any,
    settings: Any,
    clock: FrozenClock,
    setup: dict[str, Any],
    **overrides: Any,
) -> Any:
    url = overrides.pop("unsubscribe_url", f"{settings.public_base_url}/u/tok-abc")
    locale = overrides.pop("locale", "ar")
    body = overrides.pop("body_text", None) or good_body(settings, url, locale)
    kwargs: dict[str, Any] = {
        "company": setup["company"],
        "contact": setup["contact"],
        "campaign": setup["campaign"],
        "prospect": setup["prospect"],
        "mailbox": setup["mailbox"],
        "message": None,
        "channel": "email",
        "step": TemplateStep.INITIAL,
        "locale": locale,
        "subject": "ProcureAI لمصنعكم",
        "body_text": body,
        "unsubscribe_url": url,
        "settings": settings,
        "clock": clock,
        "claims_report": {"passed": True},
    }
    kwargs.update(overrides)
    return await send_gate.build_context(session, **kwargs)


async def test_clean_message_passes_all_rules(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    failed = [(c.rule, c.reason) for c in report.checks if not c.passed]
    assert report.passed, failed
    assert len(report.checks) == 14
    assert [c.rule for c in report.checks] == [
        "kill_switch",
        "mode",
        "suppression",
        "channel",
        "contact_eligible",
        "one_conversation",
        "step_logic",
        "send_window",
        "rate_limits",
        "breaker",
        "content",
        "claims",
        "budget",
        "idempotency",
    ]
    payload = report.as_dict()
    assert payload["passed"] is True
    assert payload["evaluated_at"]


@pytest.mark.parametrize(
    ("message_text", "expected_rule", "expected_reason_part"),
    [
        (None, "suppression", "suppressed"),
        (None, "kill_switch", "engaged"),
        (None, "content", "missing_unsubscribe_link"),
        (None, "content", "spam_lexicon"),
        (None, "claims", "claim_check"),
    ],
)
async def test_each_block_path(
    session: Any,
    settings: Any,
    clock: FrozenClock,
    gate_setup: Any,
    message_text: str | None,
    expected_rule: str,
    expected_reason_part: str,
    request: Any,
) -> None:
    del message_text
    scenario = request.node.callspec.params
    overrides: dict[str, Any] = {}
    if expected_reason_part.startswith("suppressed"):
        await suppression.add(
            session,
            kind="email",
            value="procurement-" + gate_setup["company"].id[:6] + "@example.com",
            reason="unsubscribe",
        )
    elif expected_rule == "kill_switch":
        await settings_service.set_kill_switch(session, True, actor="test", reason="test")
    elif expected_reason_part == "missing_unsubscribe_link":
        url = f"{settings.public_base_url}/u/tok-abc"
        overrides["body_text"] = good_body(settings, url).replace(url, "")
    elif expected_reason_part == "spam_lexicon":
        overrides["body_text"] = good_body(settings, f"{settings.public_base_url}/u/tok-abc") + " عرض محدود"
    elif expected_reason_part == "claim_check":
        overrides["claims_report"] = {"passed": False, "reason": "claim_check_failed", "issues": []}
    assert scenario is not None
    ctx = await build(session, settings, clock, gate_setup, **overrides)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert not report.passed
    matching = [c for c in report.checks if c.rule == expected_rule]
    failed = [(c.rule, c.reason) for c in report.checks if not c.passed]
    assert matching, failed
    assert not matching[0].passed, failed
    assert expected_reason_part in matching[0].reason


async def test_sandbox_only_reaches_allowlist(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    ctx = await build(session, settings, clock, gate_setup)
    ctx.mode = "sandbox"
    ctx.sandbox_allowlist = ["owner@mantiq.example"]
    report = await send_gate.evaluate(session, ctx, clock, settings)
    mode_check = next(c for c in report.checks if c.rule == "mode")
    assert not mode_check.passed
    assert mode_check.permanent
    assert mode_check.reason == "sandbox_recipient_not_allowed"

    ctx.sandbox_allowlist = [ctx.to_email]
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "mode").passed


async def test_domain_suppression_is_inherited(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    await suppression.add(session, kind="domain", value="example.com", reason="manual")
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "suppression")
    assert not check.passed
    assert "domain" in check.reason


async def test_whatsapp_blocked_without_opt_in(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["contact"] = await make_contact(
        session, gate_setup["company"].id, email="wa@example.com", phone="+201234567890"
    )
    ctx = await build(session, settings, clock, gate_setup, channel="whatsapp", step=TemplateStep.INITIAL)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    channel = next(c for c in report.checks if c.rule == "channel")
    assert not channel.passed
    assert channel.reason == "whatsapp_requires_opt_in"


async def test_whatsapp_with_opt_in_and_approved_template_passes_channel(
    session: Any, settings: Any, clock: FrozenClock, gate_setup: Any
) -> None:
    contact = await make_contact(session, gate_setup["company"].id, email="wa2@example.com", phone="+201234567891")
    gate_setup["contact"] = contact
    await consent.record_opt_in(
        session, phone="+201234567891", source="form", evidence={"form_url": "https://x.example/form"}, clock=clock
    )
    ctx = await build(session, settings, clock, gate_setup, channel="whatsapp")
    ctx.opt_in = True
    ctx.template_approved = True
    channel = await send_gate.check_channel(session, ctx, clock)
    assert channel.passed


async def test_bounced_contact_is_blocked(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["contact"].email_status = "bounced"
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "contact_eligible")
    assert not check.passed
    assert check.reason == "email_status_bounced"


async def test_risky_allowed_only_when_campaign_opts_in(
    session: Any, settings: Any, clock: FrozenClock, gate_setup: Any
) -> None:
    gate_setup["contact"].email_status = "risky"
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert not next(c for c in report.checks if c.rule == "contact_eligible").passed
    gate_setup["campaign"].allow_risky_emails = True
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "contact_eligible").passed


async def test_icp_below_campaign_threshold(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["company"].icp_score = 10
    gate_setup["campaign"].min_icp_score = 50
    await session.flush()
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "icp_threshold")
    assert not check.passed
    assert "icp_below_threshold:10<50" in check.reason


async def test_paused_campaign_blocks(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["campaign"].status = "paused"
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "campaign_active").reason == "campaign_paused"


async def test_second_contact_needs_quiet_week(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    other_contact = await make_contact(session, gate_setup["company"].id, email="other@example.com")
    second_campaign = await make_campaign(session, name="Second touch")
    other = await make_prospect(
        session,
        second_campaign,
        gate_setup["company"],
        other_contact,
        state="step1_sent",
        last_step_sent_at=clock.now() - timedelta(days=2),
    )
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "one_conversation")
    assert not check.passed
    assert check.reason == "another_thread_open_at_company"

    other.last_step_sent_at = clock.now() - timedelta(days=8)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "one_conversation").passed


async def test_followup_gap_enforced(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    prospect = gate_setup["prospect"]
    prospect.step_index = 1
    prospect.last_step_sent_at = clock.now() - timedelta(days=1)
    ctx = await build(session, settings, clock, gate_setup, step=TemplateStep.FOLLOWUP_1)
    ctx.subject = "ProcureAI follow-up"
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "step_logic")
    assert not check.passed
    assert check.reason.startswith("followup_gap_not_met")
    assert check.detail["retry_in_hours"] >= 40

    prospect.last_step_sent_at = clock.now() - timedelta(days=3, hours=1)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "step_logic").passed


async def test_replied_prospect_is_terminal(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["prospect"].replied_at = clock.now() - timedelta(hours=2)
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "step_logic").reason == "prospect_replied"


async def test_do_not_contact_is_terminal(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["prospect"].state = "do_not_contact"
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "step_logic")
    assert not check.passed
    assert check.permanent


async def test_send_window_blocks_friday_cairo(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    friday = FrozenClock(datetime(2026, 1, 9, 12, 0, tzinfo=UTC))  # Friday 14:00 Cairo
    gate_setup["campaign"].send_windows = {"weekdays": [6, 0, 1, 2, 3], "start": "09:00", "end": "17:00"}
    ctx = await build(session, settings, friday, gate_setup)
    report = await send_gate.evaluate(session, ctx, friday, settings)
    check = next(c for c in report.checks if c.rule == "send_window")
    assert not check.passed
    assert check.reason == "outside_weekdays"
    assert check.detail["next_start"]


async def test_send_window_blocks_outside_hours(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    night = FrozenClock(datetime(2026, 1, 6, 2, 0, tzinfo=UTC))  # 04:00 Cairo
    gate_setup["campaign"].send_windows = {"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "09:00", "end": "17:00"}
    ctx = await build(session, settings, night, gate_setup)
    report = await send_gate.evaluate(session, ctx, night, settings)
    check = next(c for c in report.checks if c.rule == "send_window")
    assert not check.passed
    assert check.reason in {"outside_hours", "outside_send_window"}


async def test_campaign_daily_cap(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    campaign = gate_setup["campaign"]
    campaign.daily_cap = 1
    campaign.ramp_schedule = None
    message = OutreachMessage(
        campaign_prospect_id=gate_setup["prospect"].id,
        company_id=gate_setup["company"].id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="s",
        body_text="b",
        to_domain="example.com",
        status="sent",
        sent_at=clock.now() - timedelta(hours=3),
        idempotency_key="existing-key-1",
    )
    session.add(message)
    await session.flush()
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "campaign_cap")
    assert not check.passed
    assert check.reason == "campaign_daily_cap:1/1"


async def test_ramp_schedule_reduces_cap(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    campaign = gate_setup["campaign"]
    campaign.daily_cap = 40
    campaign.ramp_schedule = {
        "start_date": (clock.now() - timedelta(days=2)).date().isoformat(),
        "steps": [{"through_day": 3, "cap": 10}, {"through_day": 10, "cap": 25}],
    }
    assert send_gate.campaign_daily_cap(campaign, clock.now()) == 10
    later = clock.now() + timedelta(days=6)
    assert send_gate.campaign_daily_cap(campaign, later) == 25


async def test_mailbox_gap_enforced(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["mailbox"].last_sent_at = clock.now() - timedelta(seconds=5)
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "mailbox_gap")
    assert not check.passed
    assert check.reason == "min_gap_between_sends"


async def test_mailbox_warmup_cap_enforced(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    mailbox = gate_setup["mailbox"]
    mailbox.warmup_stage = 0
    mailbox.created_at = clock.now()
    ctx = await build(session, settings, clock, gate_setup)
    capacity = await send_gate.mailbox_service.capacity(session, mailbox, clock)
    for index in range(capacity.warmup_cap):
        session.add(
            OutreachMessage(
                campaign_prospect_id=None,
                company_id=gate_setup["company"].id,
                channel="email",
                direction="outbound",
                step="initial",
                subject="s",
                body_text="b",
                to_domain="example.com",
                mailbox_id=mailbox.id,
                status="sent",
                sent_at=clock.now() - timedelta(minutes=30),
                idempotency_key=f"warmup-fill-{index}",
            )
        )
    await session.flush()
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "mailbox_cap")
    assert not check.passed


async def test_domain_hourly_cap(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    for index in range(2):
        session.add(
            OutreachMessage(
                company_id=gate_setup["company"].id,
                channel="email",
                direction="outbound",
                step="initial",
                subject="s",
                body_text="b",
                to_domain="example.com",
                status="sent",
                sent_at=clock.now() - timedelta(minutes=10),
                idempotency_key=f"domain-fill-{index}",
            )
        )
    await session.flush()
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "domain_cap")
    assert not check.passed
    assert check.reason == "domain_hourly_cap:2/2"


async def test_breaker_stopped_campaign(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["campaign"].status = "stopped_by_circuit_breaker"
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert next(c for c in report.checks if c.rule == "breaker").reason == "campaign_stopped_by_breaker"


async def test_global_stop_blocks_everything(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    await settings_service.set_global_stop(session, True, reason="test_stop", actor="test")
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert not next(c for c in report.checks if c.rule == "circuit_breaker").passed


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda body, settings: body.replace("ProcureAI", "our tool"), "missing_product_context"),
        (lambda body, settings: body.replace(settings.company_address, "somewhere"), "missing_postal_address"),
        (lambda body, settings: body.replace(settings.company_phone, ""), "missing_company_phone"),
        (lambda body, settings: body.replace(settings.sender_name, "someone"), "missing_identity"),
        (lambda body, settings: body.replace("إيقاف", "شكرا"), "missing_opt_out_line"),
        (lambda body, settings: body + "!!", "too_many_exclamations"),
        (lambda body, settings: body + " " + "كلمة " * 200, "body_too_long"),
        (lambda body, settings: body + " https://bit.ly/abc", "url_shortener"),
        (lambda body, settings: body + " https://evil.example.com/x", "url_not_allowed"),
    ],
)
async def test_content_rules(
    session: Any, settings: Any, clock: FrozenClock, gate_setup: Any, mutate: Any, problem: str
) -> None:
    url = f"{settings.public_base_url}/u/tok-abc"
    body = mutate(good_body(settings, url), settings)
    ctx = await build(session, settings, clock, gate_setup, body_text=body)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "content")
    assert not check.passed, check.reason
    assert problem in check.reason


async def test_all_caps_subject_and_long_subject(
    session: Any, settings: Any, clock: FrozenClock, gate_setup: Any
) -> None:
    ctx = await build(session, settings, clock, gate_setup, subject="BUY NOW FROM OUR FACTORY TODAY")
    report = await send_gate.evaluate(session, ctx, clock, settings)
    reason = next(c for c in report.checks if c.rule == "content").reason
    assert "subject_all_caps" in reason
    ctx = await build(session, settings, clock, gate_setup, subject="x" * 90)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    assert "subject_too_long" in next(c for c in report.checks if c.rule == "content").reason


async def test_idempotency_blocks_duplicate(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    ctx = await build(session, settings, clock, gate_setup)
    key = send_gate.idempotency_key(gate_setup["prospect"].id, TemplateStep.INITIAL, "email")
    session.add(
        OutreachMessage(
            campaign_prospect_id=gate_setup["prospect"].id,
            company_id=gate_setup["company"].id,
            channel="email",
            direction="outbound",
            step="initial",
            subject="s",
            body_text="b",
            to_domain="example.com",
            status="queued",
            idempotency_key=key,
        )
    )
    await session.flush()
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "idempotency")
    assert not check.passed
    assert check.reason == "already_sent_or_queued"


async def test_budget_exhaustion_blocks(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    from app.services import budgets

    await budgets.record_api_cost(session, provider="llm", cost_usd=999.0, clock=clock, unit="call")
    await session.flush()
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "budget")
    assert not check.passed
    assert check.reason == "budget_exceeded"


async def test_no_contact_blocks(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    ctx = await build(session, settings, clock, gate_setup, contact=None)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "contact_eligible")
    assert not check.passed
    assert check.reason == "no_contact"


async def test_excluded_company_blocks(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    gate_setup["company"].excluded_reason = "competitor"
    ctx = await build(session, settings, clock, gate_setup)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    check = next(c for c in report.checks if c.rule == "company_eligible")
    assert not check.passed
    assert "competitor" in check.reason


async def test_report_marks_message_blocked(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    message = OutreachMessage(
        campaign_prospect_id=gate_setup["prospect"].id,
        company_id=gate_setup["company"].id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="short",
        to_domain="example.com",
        status="draft",
        idempotency_key="msg-1",
    )
    session.add(message)
    await session.flush()
    await suppression.add(
        session,
        kind="email",
        value="procurement-" + gate_setup["company"].id[:6] + "@example.com",
        reason="unsubscribe",
    )
    ctx = await build(session, settings, clock, gate_setup, message=message)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    send_gate.apply_report_to_message(message, report, clock)
    assert message.status == "blocked"
    assert message.blocked_reason
    assert message.check_report["passed"] is False


async def test_report_queues_message(session: Any, settings: Any, clock: FrozenClock, gate_setup: Any) -> None:
    url = f"{settings.public_base_url}/u/tok-abc"
    message = OutreachMessage(
        campaign_prospect_id=gate_setup["prospect"].id,
        company_id=gate_setup["company"].id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI لمصنعكم",
        body_text=good_body(settings, url),
        to_domain="example.com",
        status="draft",
        idempotency_key="msg-2",
    )
    session.add(message)
    await session.flush()
    ctx = await build(session, settings, clock, gate_setup, message=message)
    report = await send_gate.evaluate(session, ctx, clock, settings)
    send_gate.apply_report_to_message(message, report, clock)
    assert report.passed, [(c.rule, c.reason) for c in report.checks if not c.passed]
    assert message.status == "queued"
    assert message.blocked_reason is None
    assert message.check_report["passed"] is True
