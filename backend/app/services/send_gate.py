"""THE SEND GATE.

Deterministic. Every one of the twelve rules runs on every send attempt, immediately before
queueing and again immediately before the mail leaves. The report of every rule is stored on the
message (`check_report`). There is no override flag, no admin bypass, and no "just for testing"
path: tests exercise the gate with real rows, and sandbox mode sends only to the allow-list.

Permanent failures move the prospect to a terminal state. Transient ones (window, caps, gaps)
reschedule `next_action_at` and are retried later.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import BudgetExceeded
from app.core.text import domain_of_email, word_count
from app.core.yamlconf import ClaimsConfig, load_claims
from app.db.models import (
    Campaign,
    CampaignProspect,
    Company,
    Contact,
    Mailbox,
    OutreachMessage,
)
from app.db.models.enums import (
    CampaignStatus,
    Channel,
    EmailStatus,
    MailboxStatus,
    MessageStatus,
    ProspectState,
    TemplateStep,
)
from app.services import (
    budgets,
    claim_check,
    consent,
    scheduling,
    settings_service,
    suppression,
)
from app.services import (
    mailboxes as mailbox_service,
)
from app.services.contacts import contact_email, contact_phone

SPAM_LEXICON = (
    "act now",
    "limited time",
    "risk-free",
    "no obligation",
    "make money",
    "guaranteed",
    "cash bonus",
    "100% free",
    "click here",
    "buy now",
    "عرض محدود",
    "اربح",
    "مضمون",
    "بدون التزام",
)
REASON_MARKERS = (
    "بنينا",
    "we built",
    "بتابع",
    "following up",
    "بخصوص",
    "about",
    "بشأن",
    "regarding",
    "فكرة",
    "idea",
    "مش هعمل متابعة",
    "closing the loop",
    "شكرًا على",
    "thank you",
)
TERMINAL_STATES = {
    ProspectState.DO_NOT_CONTACT,
    ProspectState.CLOSED_LOST,
    ProspectState.BOUNCED,
    ProspectState.LOST,
    ProspectState.WON,
    ProspectState.EXCLUDED,
    ProspectState.NO_RESPONSE,
    ProspectState.REFERRAL_FOLLOWED,
}
SENT_STATES = {
    ProspectState.STEP1_SENT,
    ProspectState.FOLLOWUP_1_SENT,
    ProspectState.FOLLOWUP_2_SENT,
}
REPLY_STEPS = {
    TemplateStep.REPLY_INFO,
    TemplateStep.REPLY_BOOKING,
    TemplateStep.REPLY_UNSUBSCRIBE,
    TemplateStep.REPLY_REFERRAL_THANKS,
}
MIN_GAP_SECONDS = 45
MAX_GAP_SECONDS = 240
MAX_LINKS = 3
MAX_SUBJECT_LEN = 70
INITIAL_MAX_WORDS = 140
FOLLOWUP_MAX_WORDS = 120
DOMAIN_HOURLY_CAP = 2
SECOND_CONTACT_QUIET_DAYS = 7


@dataclass
class GateCheck:
    rule: str
    passed: bool
    reason: str = ""
    permanent: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "passed": self.passed,
            "reason": self.reason,
            "permanent": self.permanent,
            "detail": self.detail,
        }


@dataclass
class GateReport:
    checks: list[GateCheck] = field(default_factory=list)
    evaluated_at: datetime | None = None

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def blocked_reason(self) -> str | None:
        failures = self.failures
        if not failures:
            return None
        first = failures[0]
        return f"{first.rule}:{first.reason}"

    @property
    def permanent(self) -> bool:
        return any(c.permanent for c in self.failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocked_reason": self.blocked_reason,
            "permanent": self.permanent,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "checks": [c.as_dict() for c in self.checks],
        }


@dataclass
class GateContext:
    campaign: Campaign | None
    prospect: CampaignProspect | None
    company: Company
    contact: Contact | None
    mailbox: Mailbox | None
    message: OutreachMessage | None
    channel: str
    step: str
    locale: str
    subject: str
    body_text: str
    to_email: str
    to_phone: str
    unsubscribe_url: str
    mode: str
    opt_in: bool = False
    in_window: bool = False
    template_approved: bool = False
    claims_report: dict[str, Any] | None = None
    claims: ClaimsConfig | None = None
    sandbox_allowlist: list[str] = field(default_factory=list)
    check_only: bool = False


def idempotency_key(prospect_id: str | None, step: str, channel: str) -> str:
    raw = f"{prospect_id or 'none'}|{step}|{channel}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>()\"]+", text or "")


def _allowed_hosts(settings: Settings, claims: ClaimsConfig) -> set[str]:
    hosts = claim_check.default_allowed_hosts(settings)
    for value in claims.owner_fill_in.values():
        if isinstance(value, str) and value.startswith("http"):
            host = (urlparse(value).hostname or "").lower()
            if host:
                hosts.add(host)
    return hosts


async def check_mode(ctx: GateContext) -> GateCheck:
    """Mode gate: `sandbox` may only reach the operator's own allow-listed inboxes."""
    if ctx.mode == "sandbox":
        target = (ctx.to_email or ctx.to_phone or "").strip().lower()
        allowed = {item.strip().lower() for item in ctx.sandbox_allowlist}
        if target and target in allowed:
            return GateCheck("mode", True, "sandbox_recipient_allowed")
        return GateCheck(
            "mode",
            False,
            "sandbox_recipient_not_allowed",
            permanent=True,
            detail={"target_domain": domain_of_email(target) if "@" in target else ""},
        )
    if ctx.mode in ("dry_run", "live"):
        return GateCheck("mode", True, ctx.mode)
    return GateCheck("mode", False, "unknown_mode", permanent=True)


async def check_kill_switch(session: AsyncSession, ctx: GateContext, clock: Clock) -> GateCheck:
    if await settings_service.kill_switch_active(session):
        return GateCheck("kill_switch", False, "engaged", permanent=False)
    stop = await settings_service.global_stop(session)
    if stop:
        return GateCheck("circuit_breaker", False, f"global_stop:{stop.get('reason', '')}"[:180], permanent=False)
    return GateCheck("kill_switch", True)


CONFIRMATION_STEPS = {TemplateStep.REPLY_UNSUBSCRIBE}


def _is_opt_out_confirmation(ctx: GateContext) -> bool:
    """One-line "your opt-out is processed" reply is allowed after suppression is recorded.

    It is a compliance acknowledgement of a message the person just sent us — not marketing — and
    it is limited to: the unsubscribe_confirmation step, an existing 1:1 thread (in_reply_to set),
    and a prospect already in DO_NOT_CONTACT. Nothing else may reach a suppressed address.
    """
    if str(ctx.step) not in {str(s) for s in CONFIRMATION_STEPS}:
        return False
    if ctx.message is None or not ctx.message.in_reply_to:
        return False
    return ctx.prospect is not None and str(ctx.prospect.state) == str(ProspectState.DO_NOT_CONTACT)


async def check_suppression(session: AsyncSession, ctx: GateContext) -> GateCheck:
    match = await suppression.is_suppressed(
        session, email=ctx.to_email or None, phone=ctx.to_phone or None
    )
    if match is not None and _is_opt_out_confirmation(ctx):
        return GateCheck("suppression", True, "opt_out_confirmation_allowed")
    if match is not None:
        return GateCheck(
            "suppression",
            False,
            match.as_reason(),
            permanent=True,
            detail={"kind": match.kind, "reason": match.reason},
        )
    return GateCheck("suppression", True)


async def check_channel(session: AsyncSession, ctx: GateContext, clock: Clock) -> GateCheck:
    if ctx.channel == Channel.EMAIL:
        return GateCheck("channel", True, "email")
    if ctx.channel != Channel.WHATSAPP:
        return GateCheck("channel", False, "unknown_channel", permanent=True)
    if ctx.in_window or await consent.window_open(session, ctx.to_phone, clock):
        return GateCheck("channel", True, "whatsapp_in_window")
    if ctx.opt_in and ctx.template_approved:
        return GateCheck("channel", True, "whatsapp_template_opt_in")
    return GateCheck(
        "channel",
        False,
        "whatsapp_requires_opt_in",
        permanent=True,
        detail={"opt_in": ctx.opt_in, "template_approved": ctx.template_approved},
    )


async def check_eligibility(session: AsyncSession, ctx: GateContext) -> GateCheck:
    if ctx.campaign is not None and ctx.campaign.status != CampaignStatus.ACTIVE:
        return GateCheck("campaign_active", False, f"campaign_{ctx.campaign.status}", permanent=False)
    if ctx.company.excluded_reason:
        return GateCheck("company_eligible", False, f"excluded:{ctx.company.excluded_reason}", permanent=True)
    if ctx.contact is None:
        return GateCheck("contact_eligible", False, "no_contact", permanent=True)
    allowed = {EmailStatus.VALID, EmailStatus.UNVERIFIED}
    if ctx.campaign is not None and ctx.campaign.allow_risky_emails:
        allowed.add(EmailStatus.RISKY)
    if ctx.contact.email_status not in allowed:
        return GateCheck(
            "contact_eligible", False, f"email_status_{ctx.contact.email_status}", permanent=True
        )
    if ctx.campaign is not None and ctx.company.icp_score < ctx.campaign.min_icp_score:
        return GateCheck(
            "icp_threshold",
            False,
            f"icp_below_threshold:{ctx.company.icp_score}<{ctx.campaign.min_icp_score}",
            permanent=False,
        )
    return GateCheck("contact_eligible", True, ctx.contact.email_status)


async def check_one_conversation(session: AsyncSession, ctx: GateContext, clock: Clock) -> GateCheck:
    """Only one open conversation per company across campaigns; second contact after 7 quiet days."""
    if ctx.prospect is None or ctx.campaign is None:
        return GateCheck("one_conversation", True, "no_prospect")
    open_states = [*SENT_STATES, ProspectState.HANDED_OFF, ProspectState.INTERESTED, ProspectState.MEETING_PROPOSAL]
    result = await session.execute(
        select(CampaignProspect).where(
            CampaignProspect.company_id == ctx.company.id,
            CampaignProspect.id != ctx.prospect.id,
            CampaignProspect.state.in_([str(s) for s in open_states]),
            CampaignProspect.replied_at.is_(None),
        )
    )
    others = list(result.scalars().all())
    if not others:
        return GateCheck("one_conversation", True, "only_thread")
    now = clock.now()
    for other in others:
        last = other.last_step_sent_at or other.created_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if last is None:
            continue
        quiet = now - last
        if quiet < timedelta(days=SECOND_CONTACT_QUIET_DAYS):
            return GateCheck(
                "one_conversation",
                False,
                "another_thread_open_at_company",
                permanent=False,
                detail={"other_prospect_id": other.id, "quiet_days": quiet.days},
            )
    return GateCheck("one_conversation", True, "allowed_second_contact_after_quiet_period")


async def check_step_logic(session: AsyncSession, ctx: GateContext, clock: Clock) -> GateCheck:
    step = ctx.step
    if step in REPLY_STEPS:
        return GateCheck("step_logic", True, "reply_step")
    prospect = ctx.prospect
    if prospect is None:
        # A dashboard preview for a company that is not in the campaign yet is informational,
        # not a send: the rest of the gate still runs and is reported.
        if ctx.check_only:
            return GateCheck("step_logic", True, "check_only_no_prospect")
        return GateCheck("step_logic", False, "no_prospect", permanent=True)
    if prospect.state in TERMINAL_STATES:
        return GateCheck("step_logic", False, f"prospect_terminal:{prospect.state}", permanent=True)
    if prospect.replied_at is not None:
        return GateCheck("step_logic", False, "prospect_replied", permanent=True)
    max_steps = 1 + (ctx.campaign.max_followups if ctx.campaign else 2)
    if prospect.step_index >= max_steps:
        return GateCheck("step_logic", False, f"max_steps_reached:{prospect.step_index}", permanent=True)
    if prospect.last_step_sent_at is not None and prospect.step_index > 0:
        gaps = (ctx.campaign.followup_gaps_days if ctx.campaign else [3, 7]) or [3]
        gap_days = gaps[min(prospect.step_index - 1, len(gaps) - 1)]
        sent_at = prospect.last_step_sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        elapsed = clock.now() - sent_at
        if elapsed < timedelta(days=gap_days):
            remaining = timedelta(days=gap_days) - elapsed
            return GateCheck(
                "step_logic",
                False,
                f"followup_gap_not_met:{gap_days}d",
                permanent=False,
                detail={"retry_in_hours": max(1, int(remaining.total_seconds() // 3600))},
            )
    sent_count = await session.execute(
        select(func.count())
        .select_from(OutreachMessage)
        .where(
            OutreachMessage.campaign_prospect_id == prospect.id,
            OutreachMessage.direction == "outbound",
            OutreachMessage.sent_at.is_not(None),
        )
    )
    if int(sent_count.scalar_one()) >= max_steps:
        return GateCheck("step_logic", False, "max_messages_reached", permanent=True)
    return GateCheck("step_logic", True, f"step_index={prospect.step_index}")


async def check_send_window(ctx: GateContext, clock: Clock) -> GateCheck:
    if ctx.channel == Channel.WHATSAPP and ctx.in_window:
        return GateCheck("send_window", True, "whatsapp_window_bypasses_business_hours")
    tz = (ctx.campaign.timezone if ctx.campaign else None) or scheduling.country_timezone(ctx.company.country)
    windows = ctx.campaign.send_windows if ctx.campaign else None
    ok, reason, next_start = scheduling.can_send_now(
        clock.now(), tz=tz, windows=windows, country=ctx.company.country
    )
    if ok:
        return GateCheck("send_window", True, f"in_window:{tz}")
    return GateCheck(
        "send_window",
        False,
        reason,
        permanent=False,
        detail={"timezone": tz, "next_start": next_start.isoformat() if next_start else None},
    )


def campaign_daily_cap(campaign: Campaign | None, now: datetime) -> int:
    if campaign is None:
        return 0
    ramp = campaign.ramp_schedule or {}
    steps = ramp.get("steps") if isinstance(ramp, dict) else None
    if not steps:
        return campaign.daily_cap
    started_raw = ramp.get("start_date")
    try:
        started = datetime.fromisoformat(str(started_raw)).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return campaign.daily_cap
    day = max(0, (now - started).days)
    for step in steps:
        if day <= int(step.get("through_day", 0)):
            return min(campaign.daily_cap, int(step.get("cap", campaign.daily_cap)))
    return campaign.daily_cap


async def check_rate_limits(session: AsyncSession, ctx: GateContext, clock: Clock) -> GateCheck:
    now = clock.now()
    if ctx.campaign is not None and ctx.prospect is not None:
        cap = campaign_daily_cap(ctx.campaign, now)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent = await session.execute(
            select(func.count())
            .select_from(OutreachMessage)
            .join(CampaignProspect, CampaignProspect.id == OutreachMessage.campaign_prospect_id)
            .where(
                CampaignProspect.campaign_id == ctx.campaign.id,
                OutreachMessage.sent_at.is_not(None),
                OutreachMessage.sent_at >= day_start,
            )
        )
        used = int(sent.scalar_one())
        if used >= cap:
            return GateCheck(
                "campaign_cap",
                False,
                f"campaign_daily_cap:{used}/{cap}",
                permanent=False,
                detail={"cap": cap, "used": used},
            )
    if ctx.mailbox is not None:
        capacity = await mailbox_service.capacity(session, ctx.mailbox, clock)
        if not capacity.can_send:
            return GateCheck(
                "mailbox_cap",
                False,
                f"mailbox_cap:{capacity.sent_today}/{capacity.warmup_cap}",
                permanent=False,
            )
        if ctx.mailbox.last_sent_at is not None:
            last = ctx.mailbox.last_sent_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            elapsed = (now - last).total_seconds()
            target_gap = MIN_GAP_SECONDS + (
                int(hashlib.sha256((ctx.to_email or "x").encode()).hexdigest(), 16)
                % (MAX_GAP_SECONDS - MIN_GAP_SECONDS)
            )
            if elapsed < min(target_gap, MIN_GAP_SECONDS * 2):
                return GateCheck(
                    "mailbox_gap",
                    False,
                    "min_gap_between_sends",
                    permanent=False,
                    detail={"elapsed_s": int(elapsed), "target_gap_s": target_gap},
                )
    if ctx.to_email:
        domain = domain_of_email(ctx.to_email)
        hour_ago = now - timedelta(hours=1)
        domain_count = await session.execute(
            select(func.count())
            .select_from(OutreachMessage)
            .where(
                OutreachMessage.to_domain == domain,
                OutreachMessage.sent_at.is_not(None),
                OutreachMessage.sent_at >= hour_ago,
            )
        )
        used_domain = int(domain_count.scalar_one())
        if used_domain >= DOMAIN_HOURLY_CAP:
            return GateCheck(
                "domain_cap",
                False,
                f"domain_hourly_cap:{used_domain}/{DOMAIN_HOURLY_CAP}",
                permanent=False,
            )
    return GateCheck("rate_limits", True)


async def check_breakers(session: AsyncSession, ctx: GateContext) -> GateCheck:
    if ctx.campaign is not None and ctx.campaign.status == CampaignStatus.STOPPED_BY_CIRCUIT_BREAKER:
        return GateCheck("breaker", False, "campaign_stopped_by_breaker", permanent=False)
    if ctx.mailbox is not None and ctx.mailbox.status != MailboxStatus.ACTIVE:
        return GateCheck("breaker", False, f"mailbox_{ctx.mailbox.status}", permanent=False)
    return GateCheck("breaker", True)


async def check_content(ctx: GateContext, settings: Settings) -> GateCheck:
    claims = ctx.claims or load_claims()
    body = ctx.body_text or ""
    subject = ctx.subject or ""
    problems: list[str] = []

    if len(subject) > MAX_SUBJECT_LEN:
        problems.append(f"subject_too_long:{len(subject)}")
    letters = [c for c in subject if c.isalpha()]
    if letters and sum(1 for c in subject if c.isupper()) / max(1, len(letters)) > 0.6 and len(subject) > 12:
        problems.append("subject_all_caps")
    if subject.count("!") > 1 or body.count("!") > 1:
        problems.append("too_many_exclamations")
    if any(token in body.lower() for token in SPAM_LEXICON):
        problems.append("spam_lexicon")
    if not body.strip():
        problems.append("empty_body")
    words = word_count(body)
    limit = INITIAL_MAX_WORDS if ctx.step == TemplateStep.INITIAL else FOLLOWUP_MAX_WORDS
    if words > limit:
        problems.append(f"body_too_long:{words}>{limit}")
    # A confirmation that an opt-out was processed carries no opt-out link and no pitch: asking a
    # person who just unsubscribed to unsubscribe again is nonsense. Identity, address and phone
    # stay mandatory; everything else about the message is unchanged.
    is_confirmation = str(ctx.step) == str(TemplateStep.REPLY_UNSUBSCRIBE)
    if not is_confirmation:
        if not any(marker in body.lower() for marker in REASON_MARKERS):
            problems.append("missing_reason_line")
        if claims.product_name not in body and ctx.step not in REPLY_STEPS:
            problems.append("missing_product_context")
        opt_out_tokens = ("إيقاف", "ايقاف") if ctx.locale == "ar" else ("stop", "unsubscribe")
        if not any(token in body.lower() for token in opt_out_tokens):
            problems.append("missing_opt_out_line")
        if not ctx.unsubscribe_url or ctx.unsubscribe_url not in body:
            problems.append("missing_unsubscribe_link")
    identity = [settings.sender_name, settings.company_name]
    for token in identity:
        if token and token.lower() not in body.lower():
            problems.append(f"missing_identity:{token}")
    if settings.company_address and settings.company_address.lower() not in body.lower():
        problems.append("missing_postal_address")
    if settings.company_phone and settings.company_phone not in body:
        problems.append("missing_company_phone")
    urls = _urls(body) + _urls(subject)
    if len(urls) > MAX_LINKS:
        problems.append(f"too_many_links:{len(urls)}")
    hosts = _allowed_hosts(settings, claims)
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if host in claim_check.SHORTENERS:
            problems.append("url_shortener")
        elif not any(host == h or host.endswith("." + h) for h in hosts):
            problems.append(f"url_not_allowed:{host}")
    if ctx.channel == Channel.EMAIL and not ctx.to_email:
        problems.append("missing_recipient")
    if problems:
        return GateCheck("content", False, ";".join(problems)[:300], permanent=True, detail={"problems": problems})
    return GateCheck("content", True, f"{words}_words")


async def check_claim_report(ctx: GateContext) -> GateCheck:
    report = ctx.claims_report
    if report is None:
        return GateCheck("claims", False, "no_claim_report", permanent=True)
    if not report.get("passed", False):
        return GateCheck(
            "claims",
            False,
            str(report.get("reason") or "claim_check_failed")[:180],
            permanent=True,
            detail={"issues": report.get("issues", [])[:5]},
        )
    return GateCheck("claims", True)


async def check_budget(session: AsyncSession, clock: Clock, settings: Settings) -> GateCheck:
    try:
        spend = await budgets.check(session, clock, settings)
    except BudgetExceeded as exc:
        return GateCheck("budget", False, "budget_exceeded", permanent=False, detail=exc.details)
    return GateCheck("budget", True, f"spent_today={spend.total}")


async def check_idempotency(session: AsyncSession, ctx: GateContext) -> GateCheck:
    key = idempotency_key(ctx.prospect.id if ctx.prospect else None, ctx.step, ctx.channel)
    result = await session.execute(
        select(OutreachMessage).where(
            OutreachMessage.idempotency_key == key,
            OutreachMessage.status.in_(
                [
                    MessageStatus.QUEUED,
                    MessageStatus.SENDING,
                    MessageStatus.SENT,
                    MessageStatus.DELIVERED,
                    MessageStatus.REPLIED,
                ]
            ),
        )
    )
    existing = result.scalars().first()
    if existing is not None and (ctx.message is None or existing.id != ctx.message.id):
        return GateCheck(
            "idempotency", False, "already_sent_or_queued", permanent=True, detail={"message_id": existing.id}
        )
    return GateCheck("idempotency", True, key[:12])


async def evaluate(
    session: AsyncSession, ctx: GateContext, clock: Clock, settings: Settings
) -> GateReport:
    report = GateReport(evaluated_at=clock.now())
    report.checks.append(await check_kill_switch(session, ctx, clock))
    report.checks.append(await check_mode(ctx))
    report.checks.append(await check_suppression(session, ctx))
    report.checks.append(await check_channel(session, ctx, clock))
    report.checks.append(await check_eligibility(session, ctx))
    report.checks.append(await check_one_conversation(session, ctx, clock))
    report.checks.append(await check_step_logic(session, ctx, clock))
    report.checks.append(await check_send_window(ctx, clock))
    report.checks.append(await check_rate_limits(session, ctx, clock))
    report.checks.append(await check_breakers(session, ctx))
    report.checks.append(await check_content(ctx, settings))
    report.checks.append(await check_claim_report(ctx))
    report.checks.append(await check_budget(session, clock, settings))
    report.checks.append(await check_idempotency(session, ctx))
    return report


def apply_report_to_message(message: OutreachMessage, report: GateReport, clock: Clock) -> None:
    message.check_report = report.as_dict()
    if report.passed:
        if message.status in (MessageStatus.DRAFT, MessageStatus.BLOCKED, MessageStatus.NEEDS_REVIEW):
            message.status = MessageStatus.QUEUED
        message.blocked_reason = None
    else:
        message.status = MessageStatus.BLOCKED
        message.blocked_reason = (report.blocked_reason or "blocked")[:190]
    message.updated_at = clock.now()


async def build_context(
    session: AsyncSession,
    *,
    company: Company,
    contact: Contact | None,
    campaign: Campaign | None,
    prospect: CampaignProspect | None,
    mailbox: Mailbox | None,
    message: OutreachMessage | None,
    channel: str,
    step: str,
    locale: str,
    subject: str,
    body_text: str,
    unsubscribe_url: str,
    settings: Settings,
    clock: Clock,
    claims_report: dict[str, Any] | None = None,
    claims: ClaimsConfig | None = None,
) -> GateContext:
    mode = await settings_service.effective_mode(session, settings)
    opt_in = False
    in_window = False
    phone = contact_phone(contact)
    if channel == Channel.WHATSAPP and phone:
        opt_in = await consent.has_opt_in(session, phone)
        in_window = await consent.window_open(session, phone, clock)
    return GateContext(
        campaign=campaign,
        prospect=prospect,
        company=company,
        contact=contact,
        mailbox=mailbox,
        message=message,
        channel=channel,
        step=step,
        locale=locale,
        subject=subject,
        body_text=body_text,
        to_email=contact_email(contact),
        to_phone=phone,
        unsubscribe_url=unsubscribe_url,
        mode=mode,
        opt_in=opt_in,
        in_window=in_window,
        claims_report=claims_report,
        claims=claims,
        sandbox_allowlist=settings.sandbox_recipients,
    )
