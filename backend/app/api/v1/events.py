"""Live dashboard feed: recent events plus an SSE stream (tenant-scoped by the access token)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import AnyUser, DbDep, SettingsDep, clock_dep
from app.core.security import decode_access_token
from app.db.models import OutboxEvent
from app.db.session import get_session_factory

router = APIRouter(prefix="/events", tags=["events"])


@router.get("")
async def recent(session: DbDep, user: AnyUser, after: int = 0, limit: int = 50) -> dict[str, Any]:
    del user
    from sqlalchemy import select

    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.seq > after)
        .order_by(OutboxEvent.seq.desc())
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return {
        "items": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "payload": event.payload,
                "at": event.created_at.isoformat() if event.created_at else "",
            }
            for event in reversed(list(rows.scalars().all()))
        ]
    }


@router.get("/stream")
async def stream(
    settings: SettingsDep, token: str = Query(...), after: int = 0
) -> StreamingResponse:
    clock = clock_dep()
    decode_access_token(token, settings, clock)

    async def generator() -> AsyncIterator[str]:
        last_seq = after
        factory = get_session_factory()
        for _ in range(600):  # ~10 minutes, then the client reconnects
            async with factory() as session:
                from sqlalchemy import select

                rows = await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.seq > last_seq)
                    .order_by(OutboxEvent.seq.asc())
                    .limit(50)
                )
                events = list(rows.scalars().all())
            for event in events:
                last_seq = max(last_seq, event.seq)
                payload = {
                    "seq": event.seq,
                    "kind": event.kind,
                    "entity_type": event.entity_type,
                    "entity_id": event.entity_id,
                    "payload": event.payload,
                    "at": event.created_at.isoformat() if event.created_at else "",
                }
                yield f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield ": keep-alive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
