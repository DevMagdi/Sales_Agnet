"""IMAP polling (every minute) feeding the same `inbound.ingest()` as provider webhooks.

The message parser is a pure function so it is unit-tested without a mail server; the network
client (aioimaplib) is thin and marked "wired, not witnessed" against a real provider in
docs/PROGRESS.md.
"""

from __future__ import annotations

import email
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

from app.core.clock import Clock
from app.core.config import Settings
from app.core.crypto import decrypt_text
from app.core.logging import get_logger
from app.db.models import Mailbox
from app.services.inbound import RawInbound, is_auto_reply, is_bounce
from app.services.inbound import ingest as ingest  # re-exported for callers

logger = get_logger("imap")


@dataclass
class ParsedEmail:
    raw: RawInbound
    is_bounce: bool
    bounce_kind: str
    is_auto_reply: bool


def _payload_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _body_text(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                return _payload_bytes(part).decode(part.get_content_charset() or "utf-8", errors="ignore")
        for part in message.walk():
            if part.get_content_type() == "text/html":
                return _payload_bytes(part).decode(part.get_content_charset() or "utf-8", errors="ignore")
        return ""
    return _payload_bytes(message).decode(message.get_content_charset() or "utf-8", errors="ignore")


def parse_message(raw_bytes: bytes) -> ParsedEmail:
    message = email.message_from_bytes(raw_bytes)
    received = None
    if message.get("Date"):
        try:
            received = parsedate_to_datetime(str(message.get("Date")))
            if received.tzinfo is None:
                received = received.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            received = None
    body = _body_text(message)
    headers = {str(k).lower(): str(v) for k, v in message.items()}
    references = str(message.get("References", "")).split() or []
    from_header = str(message.get("From", ""))
    raw = RawInbound(
        message_id=str(message.get("Message-ID", "")).strip() or f"<imap-{hash((from_header, body)) & 0xFFFFFFFF:x}@local>",
        from_email=_extract_address(from_header),
        from_label=from_header[:200],
        subject=str(message.get("Subject", "")),
        body_text=body,
        in_reply_to=(str(message.get("In-Reply-To", "")).strip() or None),
        references=references,
        headers=headers,
        received_at=received or datetime.now(UTC),
        channel="email",
    )
    bounced, kind = is_bounce(raw)
    return ParsedEmail(raw=raw, is_bounce=bounced, bounce_kind=kind, is_auto_reply=is_auto_reply(raw))


def _extract_address(header: str) -> str | None:
    import re

    match = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", header)
    return match.group(0).lower() if match else None


async def poll_mailbox(
    mailbox: Mailbox, *, settings: Settings, clock: Clock, limit: int = 50
) -> list[ParsedEmail]:
    """Fetch UNSEEN messages from one mailbox. Returns parsed messages (ingestion is separate)."""
    del settings
    import aioimaplib

    host = mailbox.imap_host
    if not host:
        return []
    client = aioimaplib.IMAP4_SSL(host=host, port=mailbox.imap_port or 993, timeout=30)
    await client.wait_hello_from_server()
    password = decrypt_text(mailbox.imap_password_enc)
    await client.login(mailbox.imap_username or mailbox.from_email, password)
    await client.select("INBOX")
    _, data = await client.search("UNSEEN")
    uids = data[0].split()[:limit] if data and data[0] else []
    parsed: list[ParsedEmail] = []
    for uid in uids:
        _, payload = await client.fetch(uid.decode() if isinstance(uid, bytes) else str(uid), "(RFC822)")
        for part in payload:
            if isinstance(part, (bytes, bytearray)) and b"From:" in part:
                parsed.append(parse_message(bytes(part)))
        await client.store(uid.decode() if isinstance(uid, bytes) else str(uid), "+FLAGS", "(\\Seen)")
    await client.logout()
    logger.info("imap_polled", mailbox=mailbox.id, fetched=len(parsed))
    del clock
    return parsed


async def ingest_parsed(session: Any, parsed: list[ParsedEmail], *, clock: Clock, settings: Settings) -> list[str]:
    ids: list[str] = []
    for item in parsed:
        row = await ingest(session, item.raw, clock=clock, settings=settings)
        ids.append(row.id)
    return ids
