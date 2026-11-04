"""Absolute suppression. Checked inside the Send Gate on every send, with no override path.

Values are stored as hashes only (plus a normalised form of the domain, which is not PII in the
same sense but is still not returned wholesale). Domain-level suppression blocks every address
at that domain. Rows are append-only.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import blind_index
from app.core.errors import ValidationFailed
from app.core.text import domain_of_email, email_match_key, normalise_email, normalise_phone
from app.db.models import SuppressionEntry
from app.db.models.enums import SuppressionKind, SuppressionReason
from app.services import audit


@dataclass
class SuppressionMatch:
    kind: str
    reason: str
    entry_id: str

    def as_reason(self) -> str:
        return f"{self.kind}_suppressed:{self.reason}"


def suppression_hash(kind: str, value: str) -> str:
    if kind == SuppressionKind.EMAIL:
        normalised = email_match_key(value)
    elif kind == SuppressionKind.PHONE:
        normalised = normalise_phone(value)
    else:
        normalised = normalise_email(value).lstrip("@")
    return blind_index(normalised, purpose=kind)


def normalise_value(kind: str, value: str) -> str:
    if kind == SuppressionKind.EMAIL:
        return email_match_key(value)
    if kind == SuppressionKind.PHONE:
        return normalise_phone(value)
    return normalise_email(value).lstrip("@")


async def find(session: AsyncSession, kind: str, value: str) -> SuppressionEntry | None:
    if not value:
        return None
    digest = suppression_hash(kind, value)
    result = await session.execute(
        select(SuppressionEntry).where(SuppressionEntry.kind == kind, SuppressionEntry.value_hash == digest)
    )
    return result.scalar_one_or_none()


async def is_suppressed(
    session: AsyncSession, *, email: str | None = None, phone: str | None = None, domain: str | None = None
) -> SuppressionMatch | None:
    """Check email, its domain (and sub-domains), and phone. Order matters for the reason."""
    if email:
        entry = await find(session, SuppressionKind.EMAIL, email)
        if entry is not None:
            return SuppressionMatch(kind="email", reason=entry.reason, entry_id=entry.id)
        target_domain = (domain or domain_of_email(email)).lower()
        parts = target_domain.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            domain_hit = await session.execute(
                select(SuppressionEntry).where(
                    SuppressionEntry.kind == SuppressionKind.DOMAIN,
                    SuppressionEntry.value_hash == suppression_hash(SuppressionKind.DOMAIN, candidate),
                )
            )
            row = domain_hit.scalar_one_or_none()
            if row is not None:
                return SuppressionMatch(kind="domain", reason=row.reason, entry_id=row.id)
    if domain:
        row = await find(session, SuppressionKind.DOMAIN, domain)
        if row is not None:
            return SuppressionMatch(kind="domain", reason=row.reason, entry_id=row.id)
    if phone:
        row = await find(session, SuppressionKind.PHONE, phone)
        if row is not None:
            return SuppressionMatch(kind="phone", reason=row.reason, entry_id=row.id)
    return None


async def add(
    session: AsyncSession,
    *,
    kind: str,
    value: str,
    reason: str,
    created_by: str = "system",
    source_message_id: str | None = None,
    note: str = "",
    emit: bool = True,
) -> tuple[SuppressionEntry, bool]:
    if kind not in tuple(SuppressionKind):
        raise ValidationFailed(f"invalid suppression kind: {kind}")
    if reason not in tuple(SuppressionReason):
        raise ValidationFailed(f"invalid suppression reason: {reason}")
    if not value.strip():
        raise ValidationFailed("suppression value is empty")
    normalised = normalise_value(kind, value)
    digest = blind_index(normalised, purpose=kind)
    existing = await find(session, kind, value)
    if existing is not None:
        return existing, False
    entry = SuppressionEntry(
        kind=kind,
        # The raw value is never stored: only the blind index. `value_normalized` is kept empty
        # so a database dump cannot be turned back into a contact list.
        value_normalized="",
        value_hash=digest,
        reason=reason,
        source_message_id=source_message_id,
        created_by=created_by,
        note=note,
    )
    session.add(entry)
    await session.flush()
    await audit.record(
        session,
        action="suppression_added",
        entity_type="suppression",
        entity_id=entry.id,
        actor_label=created_by,
        detail={"kind": kind, "reason": reason, "value_hash": digest[:12]},
    )
    if emit:
        await audit.emit_event(
            session,
            kind="suppression_added",
            entity_type="suppression",
            entity_id=entry.id,
            payload={"kind": kind, "reason": reason},
        )
    return entry, True


async def add_for_contact_reply(
    session: AsyncSession, *, email: str | None, phone: str | None, message_id: str | None, reason: str
) -> list[SuppressionEntry]:
    """Suppress every channel the contact used, plus their domain for unsubscribes/complaints."""
    created: list[SuppressionEntry] = []
    if email:
        entry, _ = await add(
            session,
            kind=SuppressionKind.EMAIL,
            value=email,
            reason=reason,
            created_by="system",
            source_message_id=message_id,
        )
        created.append(entry)
        if reason in (SuppressionReason.UNSUBSCRIBE, SuppressionReason.COMPLAINT, SuppressionReason.LEGAL):
            domain = domain_of_email(email)
            if domain:
                d_entry, _ = await add(
                    session,
                    kind=SuppressionKind.DOMAIN,
                    value=domain,
                    reason=reason,
                    created_by="system",
                    source_message_id=message_id,
                )
                created.append(d_entry)
    if phone:
        p_entry, _ = await add(
            session,
            kind=SuppressionKind.PHONE,
            value=phone,
            reason=reason,
            created_by="system",
            source_message_id=message_id,
        )
        created.append(p_entry)
    return created


async def bulk_import_csv(
    session: AsyncSession, content: str, *, created_by: str, reason: str = "manual"
) -> dict[str, Any]:
    """Import a do-not-contact CSV. Accepts `email`/`phone`/`domain` columns (or a bare list)."""
    if reason not in tuple(SuppressionReason):
        reason = SuppressionReason.MANUAL
    reader = csv.DictReader(io.StringIO(content))
    imported = 0
    duplicates = 0
    skipped: list[str] = []
    if reader.fieldnames is None:
        for raw in io.StringIO(content):
            value = raw.strip()
            if not value:
                continue
            kind = SuppressionKind.EMAIL if "@" in value else SuppressionKind.DOMAIN
            _, created = await add(
                session, kind=kind, value=value, reason=reason, created_by=created_by, emit=False
            )
            imported += 1 if created else 0
            duplicates += 0 if created else 1
        return {"imported": imported, "duplicates": duplicates, "skipped": skipped}
    for row in reader:
        lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if lowered.get("email"):
            kind, value = SuppressionKind.EMAIL, lowered["email"]
        elif lowered.get("phone"):
            kind, value = SuppressionKind.PHONE, lowered["phone"]
        elif lowered.get("domain"):
            kind, value = SuppressionKind.DOMAIN, lowered["domain"]
        else:
            values = [v for v in lowered.values() if v]
            if not values:
                continue
            value = values[0]
            kind = SuppressionKind.EMAIL if "@" in value else SuppressionKind.DOMAIN
        try:
            _, created = await add(
                session, kind=kind, value=value, reason=reason, created_by=created_by, emit=False
            )
        except ValidationFailed:
            skipped.append(value)
            continue
        imported += 1 if created else 0
        duplicates += 0 if created else 1
    await audit.record(
        session,
        action="suppression_bulk_import",
        entity_type="suppression",
        actor_label=created_by,
        detail={"imported": imported, "duplicates": duplicates},
    )
    return {"imported": imported, "duplicates": duplicates, "skipped": skipped}


async def count(session: AsyncSession) -> dict[str, int]:
    result = await session.execute(
        select(SuppressionEntry.kind, func.count()).group_by(SuppressionEntry.kind)
    )
    return {kind: int(total) for kind, total in result.all()}


async def list_entries(
    session: AsyncSession, *, kind: str | None = None, reason: str | None = None, limit: int = 200
) -> list[SuppressionEntry]:
    stmt = select(SuppressionEntry).order_by(SuppressionEntry.created_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(SuppressionEntry.kind == kind)
    if reason:
        stmt = stmt.where(SuppressionEntry.reason == reason)
    result = await session.execute(stmt)
    return list(result.scalars().all())
