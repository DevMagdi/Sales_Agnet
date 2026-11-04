"""The one place prospect state changes. Illegal transitions raise; every legal one is audited.

    discovered → enriched → researched → qualified → queued → composing → step1_sent
     → (followup_1_sent → followup_2_sent)* → no_response          [terminal unless a late reply]
    replies from any sent state → interested | wants_info | meeting_proposal → handed_off
     → demo_booked → demo_held → won | lost
    referral → referral_followed (original terminal); not_now → snoozed → queued (once)
    not_interested → closed_lost; unsubscribe|complaint → do_not_contact; bounce_hard → bounced
    out_of_office → paused → resumes
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.errors import ValidationFailed
from app.db.models import CampaignProspect
from app.db.models.enums import ProspectState
from app.services import audit

S = ProspectState

ALLOWED: dict[str, set[str]] = {
    S.DISCOVERED: {S.ENRICHED, S.EXCLUDED},
    S.ENRICHED: {S.RESEARCHED, S.EXCLUDED},
    S.RESEARCHED: {S.QUALIFIED, S.EXCLUDED},
    S.QUALIFIED: {S.QUEUED, S.EXCLUDED},
    S.QUEUED: {S.COMPOSING, S.BLOCKED_BY_GATE, S.EXCLUDED},
    S.COMPOSING: {S.STEP1_SENT, S.BLOCKED_BY_GATE, S.EXCLUDED, S.QUEUED},
    S.STEP1_SENT: {
        S.FOLLOWUP_1_SENT,
        S.NO_RESPONSE,
        S.INTERESTED,
        S.WANTS_INFO,
        S.MEETING_PROPOSAL,
        S.NOT_NOW,
        S.NOT_INTERESTED,
        S.DO_NOT_CONTACT,
        S.BOUNCED,
        S.PAUSED,
        S.BLOCKED_BY_GATE,
    },
    S.FOLLOWUP_1_SENT: {
        S.FOLLOWUP_2_SENT,
        S.NO_RESPONSE,
        S.INTERESTED,
        S.WANTS_INFO,
        S.MEETING_PROPOSAL,
        S.NOT_NOW,
        S.NOT_INTERESTED,
        S.DO_NOT_CONTACT,
        S.BOUNCED,
        S.PAUSED,
        S.BLOCKED_BY_GATE,
    },
    S.FOLLOWUP_2_SENT: {
        S.NO_RESPONSE,
        S.INTERESTED,
        S.WANTS_INFO,
        S.MEETING_PROPOSAL,
        S.NOT_NOW,
        S.NOT_INTERESTED,
        S.DO_NOT_CONTACT,
        S.BOUNCED,
        S.PAUSED,
    },
    S.NO_RESPONSE: {
        S.INTERESTED,
        S.WANTS_INFO,
        S.MEETING_PROPOSAL,
        S.NOT_NOW,
        S.DO_NOT_CONTACT,
        S.CLOSED_LOST,
        S.SNOOZED,
    },
    S.INTERESTED: {S.HANDED_OFF, S.DO_NOT_CONTACT, S.CLOSED_LOST, S.SNOOZED},
    S.WANTS_INFO: {S.HANDED_OFF, S.DO_NOT_CONTACT, S.CLOSED_LOST, S.STEP1_SENT},
    S.MEETING_PROPOSAL: {S.HANDED_OFF, S.DO_NOT_CONTACT, S.CLOSED_LOST},
    S.HANDED_OFF: {S.DEMO_BOOKED, S.LOST, S.WON, S.DO_NOT_CONTACT, S.SNOOZED},
    S.DEMO_BOOKED: {S.DEMO_HELD, S.LOST, S.NO_RESPONSE, S.HANDED_OFF},
    S.DEMO_HELD: {S.WON, S.LOST, S.HANDED_OFF},
    S.NOT_NOW: {S.SNOOZED, S.DO_NOT_CONTACT, S.CLOSED_LOST},
    S.NOT_INTERESTED: {S.CLOSED_LOST, S.DO_NOT_CONTACT},
    S.SNOOZED: {S.QUEUED, S.DO_NOT_CONTACT, S.CLOSED_LOST},
    S.PAUSED: {S.QUEUED, S.DO_NOT_CONTACT, S.CLOSED_LOST, S.SNOOZED, S.NO_RESPONSE},
    S.BLOCKED_BY_GATE: {S.QUEUED, S.EXCLUDED, S.CLOSED_LOST, S.DO_NOT_CONTACT},
    S.EXCLUDED: set(),
    S.DO_NOT_CONTACT: set(),
    S.CLOSED_LOST: set(),
    S.BOUNCED: set(),
    S.WON: set(),
    S.LOST: set(),
    S.REFERRAL_FOLLOWED: set(),
}

# Opt-out is a safety transition: it must be reachable from every state that is not already
# terminal, so a person who says "stop" can never be blocked by the state graph.
for _state, _targets in ALLOWED.items():
    if _targets:
        _targets.add(S.DO_NOT_CONTACT)

TERMINAL = {state for state, targets in ALLOWED.items() if not targets}


def is_legal(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        return True
    return to_state in ALLOWED.get(from_state, set())


async def transition(
    session: AsyncSession,
    prospect: CampaignProspect,
    to_state: str,
    *,
    clock: Clock,
    actor: str = "system",
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> CampaignProspect:
    if to_state not in tuple(ProspectState):
        raise ValidationFailed(f"unknown state: {to_state}")
    if not is_legal(prospect.state, to_state):
        raise ValidationFailed(
            f"illegal transition {prospect.state} → {to_state}",
            details={"prospect_id": prospect.id},
        )
    previous = prospect.state
    if previous == to_state:
        return prospect
    prospect.state = to_state
    if to_state in TERMINAL:
        prospect.next_action_at = None
        prospect.locked_until = None
        prospect.locked_by = None
    await session.flush()
    await audit.record(
        session,
        action="state_transition",
        entity_type="campaign_prospect",
        entity_id=prospect.id,
        actor_label=actor,
        detail={"from": previous, "to": to_state, "reason": reason, **({"extra": extra} if extra else {})},
        before={"state": previous},
        after={"state": to_state},
    )
    await audit.emit_event(
        session,
        kind="prospect_state",
        entity_type="campaign_prospect",
        entity_id=prospect.id,
        payload={"from": previous, "to": to_state, "reason": reason},
    )
    return prospect


def state_after_send(step: str) -> str:
    return {
        "initial": ProspectState.STEP1_SENT,
        "followup_1": ProspectState.FOLLOWUP_1_SENT,
        "followup_2": ProspectState.FOLLOWUP_2_SENT,
        "breakup": ProspectState.NO_RESPONSE,
    }.get(step, ProspectState.STEP1_SENT)


def state_for_reply(reply_class: str) -> str:
    return {
        "interested": ProspectState.INTERESTED,
        "wants_info": ProspectState.WANTS_INFO,
        "meeting_proposal": ProspectState.MEETING_PROPOSAL,
        "referral": ProspectState.INTERESTED,
        "not_now": ProspectState.NOT_NOW,
        "not_interested": ProspectState.NOT_INTERESTED,
        "unsubscribe": ProspectState.DO_NOT_CONTACT,
        "complaint": ProspectState.DO_NOT_CONTACT,
        "out_of_office": ProspectState.PAUSED,
        "auto_reply": ProspectState.PAUSED,
        "bounce": ProspectState.BOUNCED,
        "question_technical": ProspectState.WANTS_INFO,
        "question_pricing": ProspectState.WANTS_INFO,
        "unclear": ProspectState.PAUSED,
    }.get(reply_class, ProspectState.PAUSED)


def transitions_table() -> dict[str, list[str]]:
    return {state: sorted(targets) for state, targets in ALLOWED.items()}
