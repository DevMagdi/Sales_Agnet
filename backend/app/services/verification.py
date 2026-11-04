"""Email verification pipeline: syntax → disposable/free → MX → optional provider → status.

`invalid` and `disposable` addresses are never emailed. `risky` only when a campaign opts in.
Nothing here generates or guesses an address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import VerifyAdapter
from app.core.clock import Clock
from app.core.text import domain_of_email, mask_email
from app.core.yamlconf import load_blocklist
from app.db.models import Contact
from app.db.models.enums import EmailStatus
from app.services import audit
from app.services import contacts as contact_service


@dataclass
class VerificationSummary:
    checked: int = 0
    valid: int = 0
    risky: int = 0
    invalid: int = 0
    disposable: int = 0
    unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "valid": self.valid,
            "risky": self.risky,
            "invalid": self.invalid,
            "disposable": self.disposable,
            "unknown": self.unknown,
        }


def syntax_ok(email: str) -> bool:
    if not email or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain or " " in email:
        return False
    return "." in domain.split(".")[-1] or len(domain.rsplit(".", 1)[-1]) >= 2


async def verify_email(session: AsyncSession, adapter: VerifyAdapter, email: str) -> str:
    if not syntax_ok(email):
        return EmailStatus.INVALID
    blocklist = load_blocklist()
    domain = domain_of_email(email)
    if domain in blocklist.disposable:
        return EmailStatus.DISPOSABLE
    result = await adapter.verify(email)
    mapping = {
        "valid": EmailStatus.VALID,
        "invalid": EmailStatus.INVALID,
        "disposable": EmailStatus.DISPOSABLE,
        "risky": EmailStatus.RISKY,
        "unknown": EmailStatus.UNVERIFIED,
    }
    return str(mapping.get(result.status, EmailStatus.UNVERIFIED))


async def verify_contact(
    session: AsyncSession, contact: Contact, adapter: VerifyAdapter, clock: Clock
) -> str:
    email = contact_service.contact_email(contact)
    status = await verify_email(session, adapter, email)
    if status == EmailStatus.INVALID and contact.email_is_role_based:
        status = EmailStatus.RISKY if syntax_ok(email) else EmailStatus.INVALID
    contact.email_status = status
    contact.updated_at = clock.now()
    await session.flush()
    return status


async def verify_company_contacts(
    session: AsyncSession, company_id: str, adapter: VerifyAdapter, clock: Clock
) -> VerificationSummary:
    summary = VerificationSummary()
    rows = await session.execute(select(Contact).where(Contact.company_id == company_id))
    for contact in rows.scalars().all():
        status = await verify_contact(session, contact, adapter, clock)
        summary.checked += 1
        if status == "valid":
            summary.valid += 1
        elif status == "risky":
            summary.risky += 1
        elif status == "invalid":
            summary.invalid += 1
        elif status == "disposable":
            summary.disposable += 1
        else:
            summary.unknown += 1
    await audit.record(
        session,
        action="contacts_verified",
        entity_type="company",
        entity_id=company_id,
        detail=summary.as_dict(),
    )
    return summary


def exportable_status(status: str) -> bool:
    return status in (EmailStatus.VALID, EmailStatus.UNVERIFIED, EmailStatus.RISKY)


def verification_report(contact: Contact) -> dict[str, Any]:
    return {
        "contact_id": contact.id,
        "email": mask_email(contact_service.contact_email(contact)),
        "status": contact.email_status,
        "role_based": contact.email_is_role_based,
        "source_url": contact.email_source_url,
    }
