"""Public one-click unsubscribe (RFC 8058). No login, idempotent, HMAC-signed, PII-free tokens."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.deps import ClockDep, DbDep, limiter
from app.core.crypto import verify_token
from app.core.text import mask_email
from app.db.models import CampaignProspect, Contact, OutreachMessage
from app.db.models.enums import MessageStatus, ProspectState, SuppressionReason
from app.services import audit, state_machine, suppression
from app.services.contacts import contact_email

router = APIRouter(tags=["public"])

PAGE = """<!doctype html>
<html lang="{lang}" dir="{dir}">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
 body{{font-family:system-ui,Arial,sans-serif;background:#0b1b33;color:#f5f7fa;display:flex;
 min-height:100vh;align-items:center;justify-content:center;margin:0}}
 .card{{background:#fff;color:#0b1b33;padding:32px 36px;border-radius:14px;max-width:560px;
 box-shadow:0 20px 60px rgba(0,0,0,.35)}}
 h1{{margin:0 0 12px;font-size:22px;color:#0b1b33}} .gold{{color:#b8892b}}
 p{{line-height:1.7;margin:8px 0}} .muted{{color:#5b6779;font-size:13px}}
 button{{background:#0b1b33;color:#fff;border:0;padding:12px 22px;border-radius:8px;font-size:15px;
 cursor:pointer;margin-top:16px}} .ok{{color:#1d7a46;font-weight:600}}
</style></head>
<body><div class="card">
<h1>{heading}</h1>
<div class="gold">Mantiq Technologies — ProcureAI</div>
{body}
<p class="muted">{footer}</p>
</div></body></html>"""


def _page(lang: str, heading: str, body: str, ok: bool = True) -> HTMLResponse:
    title = "تم إلغاء الاشتراك" if lang == "ar" else "Unsubscribed"
    footer = (
        "لو ده حصل بالغلط، رد على أي رسالة مننا وهنراجع الموضوع."
        if lang == "ar"
        else "If this happened by mistake, reply to any of our emails and we will review it."
    )
    if not ok:
        title = "رابط غير صالح" if lang == "ar" else "Invalid link"
        footer = "الرابط منتهي أو غير صحيح." if lang == "ar" else "This link is expired or invalid."
    return HTMLResponse(
        PAGE.format(
            lang=lang,
            dir="rtl" if lang == "ar" else "ltr",
            title=title,
            heading=heading,
            body=body,
            footer=footer,
        )
    )


async def _apply(token: str, session: DbDep, clock: ClockDep) -> tuple[bool, str]:
    try:
        payload = verify_token(token)
    except ValueError:
        return False, "invalid"
    contact_id = str(payload.get("c", ""))
    contact = await session.get(Contact, contact_id)
    if contact is None:
        return True, "already"
    email = contact_email(contact)
    if email:
        await suppression.add_for_contact_reply(
            session,
            email=email,
            phone=None,
            message_id=None,
            reason=SuppressionReason.UNSUBSCRIBE,
        )
    prospects = await session.execute(
        select(CampaignProspect).where(CampaignProspect.contact_id == contact.id)
    )
    for prospect in prospects.scalars().all():
        pending = await session.execute(
            select(OutreachMessage).where(
                OutreachMessage.campaign_prospect_id == prospect.id,
                OutreachMessage.status.in_([MessageStatus.QUEUED, MessageStatus.NEEDS_REVIEW, MessageStatus.DRAFT]),
            )
        )
        for message in pending.scalars().all():
            message.status = MessageStatus.BLOCKED
            message.blocked_reason = "unsubscribed_public_link"
        if state_machine.is_legal(prospect.state, ProspectState.DO_NOT_CONTACT):
            await state_machine.transition(
                session, prospect, ProspectState.DO_NOT_CONTACT, clock=clock, reason="public_unsubscribe"
            )
        else:
            prospect.state = ProspectState.DO_NOT_CONTACT
            prospect.next_action_at = None
    await audit.record(
        session,
        action="public_unsubscribe",
        entity_type="contact",
        entity_id=contact.id,
        actor_label="public_link",
        detail={"email": mask_email(email)},
    )
    await audit.emit_event(
        session,
        kind="unsubscribed",
        entity_type="contact",
        entity_id=contact.id,
        payload={"email_masked": mask_email(email)},
    )
    await session.flush()
    return True, "unsubscribed"


@router.get("/u/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str, request: Request, session: DbDep, clock: ClockDep) -> HTMLResponse:
    limiter().check(f"unsub:{request.client.host if request.client else 'na'}", 60, 60)
    ok, detail = await _apply(token, session, clock)
    if not ok:
        return _page("en", "Invalid or expired link", "<p>This unsubscribe link is not valid.</p>", ok=False)
    body = (
        "<p>تم إيقاف المراسلات، ومش هنتواصل معاكم تاني.</p>"
        "<p class='ok'>You've been removed; we won't contact you again.</p>"
        if detail == "unsubscribed"
        else "<p class='ok'>You're already unsubscribed.</p>"
    )
    return _page("ar", "تم إلغاء الاشتراك", body)


@router.post("/u/{token}", response_class=HTMLResponse)
async def unsubscribe_one_click(token: str, session: DbDep, clock: ClockDep) -> HTMLResponse:
    """RFC 8058 List-Unsubscribe-Post target: a mail client POSTs here with no user interaction."""
    ok, detail = await _apply(token, session, clock)
    if not ok:
        return HTMLResponse(
            "<html><body>invalid unsubscribe token</body></html>", status_code=400
        )
    return _page("ar", "تم إلغاء الاشتراك", "<p class='ok'>Unsubscribed / تم إلغاء الاشتراك</p>")


@router.get("/u-ping", response_class=HTMLResponse)
async def ping() -> HTMLResponse:
    return HTMLResponse("<html><body>ok</body></html>")
