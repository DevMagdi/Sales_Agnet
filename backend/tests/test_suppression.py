"""Suppression: absolute, append-only, parent-domain inherited, immediate on opt-out."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import ValidationFailed
from app.services import suppression


async def test_email_is_suppressed(session: Any) -> None:
    await suppression.add(session, kind="email", value="Owner@Factory.example", reason="unsubscribe")
    match = await suppression.is_suppressed(session, email="owner@factory.example")
    assert match is not None
    assert match.kind == "email"
    assert await suppression.is_suppressed(session, email="other@factory.example") is None


async def test_parent_domain_covers_subdomains(session: Any) -> None:
    await suppression.add(session, kind="domain", value="factory.example", reason="complaint")
    for candidate in ("hq.factory.example", "a.b.factory.example", "factory.example"):
        match = await suppression.is_suppressed(session, email=f"x@{candidate}")
        assert match is not None, candidate
        assert match.kind == "domain", candidate
    assert await suppression.is_suppressed(session, email="x@notfactory.example") is None


async def test_phone_suppression_normalised(session: Any) -> None:
    await suppression.add(session, kind="phone", value="+20 100 111 2222", reason="unsubscribe")
    match = await suppression.is_suppressed(session, phone="+201001112222")
    assert match is not None
    assert match.kind == "phone"
    assert await suppression.is_suppressed(session, phone="01001112222") is not None


async def test_duplicate_add_is_idempotent(session: Any) -> None:
    first, created_first = await suppression.add(session, kind="email", value="a@b.example", reason="manual")
    second, created_second = await suppression.add(session, kind="email", value="A@B.example", reason="manual")
    assert created_first is True
    assert created_second is False
    assert first.id == second.id


async def test_invalid_kind_or_reason_rejected(session: Any) -> None:
    with pytest.raises(ValidationFailed):
        await suppression.add(session, kind="nonsense", value="a@b.example", reason="manual")
    with pytest.raises(ValidationFailed):
        await suppression.add(session, kind="email", value="a@b.example", reason="nonsense")
    with pytest.raises(ValidationFailed):
        await suppression.add(session, kind="email", value="   ", reason="manual")


async def test_plain_value_never_stored(session: Any) -> None:
    entry, _ = await suppression.add(session, kind="email", value="hidden@factory.example", reason="unsubscribe")
    assert "hidden@factory.example" not in (entry.value_normalized or "")  # only the hash of the normal form
    assert entry.value_hash
    assert len(entry.value_hash) >= 32


async def test_opt_out_from_reply_suppresses_email_and_phone(session: Any) -> None:
    entries = await suppression.add_for_contact_reply(
        session,
        email="reply@factory.example",
        phone="+201234567890",
        message_id=None,
        reason="unsubscribe",
    )
    assert {e.kind for e in entries} == {"email", "domain", "phone"}
    assert await suppression.is_suppressed(session, email="reply@factory.example") is not None
    assert await suppression.is_suppressed(session, phone="+201234567890") is not None


async def test_bulk_import_csv(session: Any) -> None:
    csv_text = "email\na@factory.example\nb@factory.example\n"
    report = await suppression.bulk_import_csv(session, csv_text, created_by="tester")
    assert report["imported"] == 2
    assert report["duplicates"] == 0
    assert await suppression.is_suppressed(session, email="b@factory.example") is not None


async def test_counts_and_listing(session: Any) -> None:
    await suppression.add(session, kind="email", value="x@y.example", reason="manual")
    await suppression.add(session, kind="domain", value="z.example", reason="manual")
    counts = await suppression.count(session)
    assert counts.get("email") == 1
    assert counts.get("domain") == 1
    rows = await suppression.list_entries(session, limit=10)
    assert len(rows) == 2


async def test_entries_are_append_only_in_app_layer(session: Any) -> None:
    """There is deliberately no delete path in the service layer; the DB trigger enforces it."""
    await suppression.add(session, kind="email", value="immutable@y.example", reason="manual")
    assert not hasattr(suppression, "delete_entry")
    assert not hasattr(suppression, "remove")
