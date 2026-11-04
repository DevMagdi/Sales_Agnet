"""RFC 5322 message construction shared by all email adapters."""

from __future__ import annotations

import hashlib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.adapters.base import OutboundEmail


def new_message_id(domain: str) -> str:
    return make_msgid(domain=domain or None)


def build_mime(message: OutboundEmail) -> tuple[bytes, str]:
    """Return (raw bytes, message_id)."""
    domain = message.from_email.partition("@")[2] or "localhost"
    message_id = message.message_id or new_message_id(domain)
    msg = EmailMessage()
    msg["From"] = formataddr((message.from_name, message.from_email))
    msg["To"] = message.to_email
    msg["Subject"] = message.subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    if message.reply_to:
        msg["Reply-To"] = message.reply_to
    if message.in_reply_to:
        msg["In-Reply-To"] = message.in_reply_to
    if message.references:
        msg["References"] = " ".join(message.references)
    # 1:1 business mail: no Precedence: bulk. One-click unsubscribe is mandatory (RFC 8058).
    if message.unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{message.unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    for key, value in message.headers.items():
        if value and key.lower() not in {"to", "from", "subject", "date", "message-id"}:
            msg[key] = value
    msg.set_content(message.body_text)
    if message.body_html:
        msg.add_alternative(message.body_html, subtype="html")
    return msg.as_bytes(), message_id


def stable_message_id(message: OutboundEmail, salt: str = "") -> str:
    """Deterministic Message-ID for idempotent re-sends of the same message row."""
    seed = f"{message.from_email}|{message.to_email}|{message.subject}|{salt}".encode()
    return f"<{hashlib.sha256(seed).hexdigest()[:32]}@outreach.mantiq.local>"
