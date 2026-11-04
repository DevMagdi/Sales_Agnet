"""Inbound webhooks: email provider events and WhatsApp Cloud API.

Both verify signatures before doing anything, dedupe events, and never trust the payload shape.
WhatsApp rejects unsigned calls with 401.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from sqlalchemy import select

from app.api.deps import ClockDep, DbDep, RegistryDep, SettingsDep, limiter
from app.db.models import InboundMessage, OutreachMessage, WebhookDelivery
from app.db.models.enums import MessageStatus
from app.services import audit, reply_handler
from app.services import inbound as inbound_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _record_delivery(
    session: DbDep, provider: str, event_id: str, payload: dict[str, Any]
) -> tuple[WebhookDelivery, bool]:
    existing = await session.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.provider == provider, WebhookDelivery.event_id == event_id
        )
    )
    row = existing.scalars().first()
    if row is not None:
        return row, False
    row = WebhookDelivery(provider=provider, event_id=event_id, payload=payload, processed=False)
    session.add(row)
    await session.flush()
    return row, True


@router.post("/email")
async def email_webhook(
    request: Request,
    session: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    registry: RegistryDep,
) -> dict[str, Any]:
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not registry.email.verify_webhook(raw, headers):
        await audit.record(
            session, action="webhook_rejected", entity_type="webhook", actor_label="email",
            detail={"reason": "bad_signature"},
        )
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    events = registry.email.parse_event(payload)
    event_id = str(payload.get("id") or payload.get("event_id") or json.dumps(payload, sort_keys=True)[:120])
    delivery, fresh = await _record_delivery(session, "email", event_id, payload)
    if not fresh:
        return {"ok": True, "duplicate": True, "processed": 0}
    results = []
    for event in events:
        results.append(
            await __import__("app.services.sender", fromlist=["process_email_event"]).process_email_event(
                session, event, registry=registry, clock=clock
            )
        )
    delivery.processed = True
    await session.flush()
    await audit.record(
        session, action="webhook_processed", entity_type="webhook", entity_id=delivery.id,
        actor_label="email_provider", detail={"events": len(events)},
    )
    del settings
    return {"ok": True, "processed": len(results), "results": results}


@router.get("/whatsapp")
async def whatsapp_verify(
    request: Request, settings: SettingsDep
) -> Response:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and settings.whatsapp_verify_token and token == settings.whatsapp_verify_token:
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    session: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    registry: RegistryDep,
    x_hub_signature_256: str = Header(default=""),
) -> dict[str, Any]:
    raw = await request.body()
    if not registry.whatsapp.verify_signature(raw, x_hub_signature_256):
        await audit.record(
            session, action="webhook_rejected", entity_type="webhook", actor_label="whatsapp",
            detail={"reason": "bad_signature"},
        )
        raise HTTPException(status_code=401, detail="invalid signature")
    limiter().check("webhook:whatsapp", 600, 60)
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    events = registry.whatsapp.parse_events(payload)
    processed = 0
    replies = 0
    for event in events:
        if event.type == "status" and event.message_id:
            found = await session.execute(
                select(OutreachMessage).where(OutreachMessage.provider_message_id == event.message_id)
            )
            message = found.scalars().first()
            if (
                message is not None
                and event.status in ("delivered", "read")
                and message.status == MessageStatus.SENT
            ):
                message.status = MessageStatus.DELIVERED
            processed += 1
            continue
        if event.type == "message" and event.from_phone:
            raw_inbound = inbound_service.RawInbound(
                message_id=f"wa:{event.message_id}",
                from_phone=event.from_phone,
                from_label=event.from_phone,
                body_text=event.text or "",
                channel="whatsapp",
                headers={"user-agent": "whatsapp-cloud"},
            )
            inbound_row = await inbound_service.ingest(session, raw_inbound, clock=clock, settings=settings)
            processed += 1
            if inbound_row.campaign_prospect_id:
                outcome = await reply_handler.process_inbound(
                    session, inbound_row, registry=registry, clock=clock, settings=settings
                )
                replies += 1
                processed += 0 if outcome else 0
    await session.flush()
    del clock
    return {"ok": True, "processed": processed, "replies_handled": replies}


@router.get("/health")
async def webhook_health(session: DbDep) -> dict[str, Any]:
    rows = await session.execute(select(InboundMessage.id).limit(1))
    return {"ok": True, "inbound_seen": len(list(rows.scalars().all()))}
