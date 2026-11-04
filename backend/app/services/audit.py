"""Append-only audit trail and the dashboard live-feed outbox.

One audit row per state transition, send, block, setting change and login. Rows are never
updated or deleted (enforced by DB triggers in the migration).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, OutboxEvent


def hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, default=str, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def record(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    actor_id: str | None = None,
    actor_label: str = "system",
    detail: dict[str, Any] | None = None,
    before: Any = None,
    after: Any = None,
    ip: str | None = None,
    org_id: str = "org-default",
) -> AuditLog:
    entry = AuditLog(
        org_id=org_id,
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_hash=hash_payload(before) if before is not None else None,
        after_hash=hash_payload(after) if after is not None else None,
        detail=detail or {},
        ip=ip,
    )
    session.add(entry)
    await session.flush()
    return entry


async def next_seq(session: AsyncSession) -> int:
    result = await session.execute(select(func.coalesce(func.max(OutboxEvent.seq), 0)))
    return int(result.scalar_one()) + 1


async def emit_event(
    session: AsyncSession,
    *,
    kind: str,
    entity_type: str = "",
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
    org_id: str = "org-default",
) -> OutboxEvent:
    event = OutboxEvent(
        org_id=org_id,
        seq=await next_seq(session),
        kind=kind,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event
