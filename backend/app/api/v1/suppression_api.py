"""Suppression list administration (read for sales, write for admin)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import AdminUser, AnyUser, DbDep, WriterUser
from app.api.schemas import SuppressionCreate, SuppressionImportRequest
from app.core.crypto import blind_index
from app.core.text import normalise_phone
from app.services import audit, suppression

router = APIRouter(prefix="/suppression", tags=["suppression"])


def entry_view(entry: Any) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "value_hash": entry.value_hash[:12],
        "value_stored": False,  # only the blind index exists; the raw address is never kept
        "reason": entry.reason,
        "created_by": entry.created_by,
        "source_message_id": entry.source_message_id,
        "note": entry.note,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


@router.get("")
async def list_suppression(
    session: DbDep, user: AnyUser, kind: str | None = None, reason: str | None = None, limit: int = 200
) -> dict[str, Any]:
    del user
    entries = await suppression.list_entries(session, kind=kind, reason=reason, limit=limit)
    return {"items": [entry_view(e) for e in entries], "counts": await suppression.count(session)}


@router.get("/check")
async def check(session: DbDep, user: AnyUser, email: str | None = None, phone: str | None = None) -> dict[str, Any]:
    del user
    match = await suppression.is_suppressed(session, email=email, phone=phone)
    return {"suppressed": match is not None, "kind": match.kind if match else None, "reason": match.reason if match else None}


@router.post("")
async def add_entry(payload: SuppressionCreate, session: DbDep, user: WriterUser) -> dict[str, Any]:
    entry, created = await suppression.add(
        session,
        kind=payload.kind,
        value=payload.value,
        reason=payload.reason,
        created_by=user.email,
        note=payload.note,
    )
    await audit.record(
        session, action="suppression_added_manual", entity_type="suppression", entity_id=entry.id,
        actor_id=user.id, actor_label=user.email, detail={"kind": payload.kind, "reason": payload.reason},
    )
    return {"entry": entry_view(entry), "created": created}


@router.post("/import")
async def import_csv(
    session: DbDep, user: AdminUser, payload: SuppressionImportRequest
) -> dict[str, Any]:
    result = await suppression.bulk_import_csv(
        session, payload.content, created_by=user.email, reason=payload.reason
    )
    await audit.record(
        session, action="suppression_bulk_import_api", entity_type="suppression",
        actor_id=user.id, actor_label=user.email, detail=result,
    )
    return result


@router.get("/export.csv")
async def export_csv(session: DbDep, user: AnyUser) -> Response:
    del user
    entries = await suppression.list_entries(session, limit=10_000)
    lines = ["kind,value_hash,reason,created_at,created_by"]
    for entry in entries:
        lines.append(
            f"{entry.kind},{entry.value_hash},{entry.reason},{entry.created_at.isoformat()},{entry.created_by or ''}"
        )
    return Response(
        content="\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="suppression.csv"'},
    )


def hash_hint(value: str) -> str:
    return blind_index(normalise_phone(value), purpose="phone")[:8]
