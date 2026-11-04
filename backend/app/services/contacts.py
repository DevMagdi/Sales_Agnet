"""Contact extraction and storage.

Only published business addresses are used (company website `mailto:`/visible text, imported CSV,
or an explicit referral in a reply). Addresses are never guessed or pattern-generated, and every
address keeps the URL it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import blind_index, decrypt_text, encrypt_text
from app.core.errors import ValidationFailed
from app.core.text import (
    detect_language,
    domain_of_email,
    email_match_key,
    extract_emails,
    mask_email,
    normalise_phone,
)
from app.db.models import Contact
from app.db.models.enums import EmailStatus, RoleCategory
from app.services import audit

ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"(?:^|\.)(procurement|procure|sourcing|tenders?)(?:$|[.\-_])", RoleCategory.PROCUREMENT),
    (r"(?:^|\.)(purchas\w*|buys?|buyer)(?:$|[.\-_])", RoleCategory.PURCHASING),
    (r"(?:^|\.)(supplychain|supply|logistics|warehouse)(?:$|[.\-_])", RoleCategory.SUPPLY_CHAIN),
    (r"(?:^|\.)(operations?|production|plant)(?:$|[.\-_])", RoleCategory.OPERATIONS),
    (r"(?:^|\.)(owner|gm|ceo|md|manager|director|info|contact)(?:$|[.\-_])", RoleCategory.OWNER_GM),
    (r"(?:^|\.)(finance|accounting|accounts)(?:$|[.\-_])", RoleCategory.FINANCE),
]

TITLE_PATTERNS: list[tuple[str, str]] = [
    (r"مشتريات|procurement|purchas|sourcing|buyer|مشتريات", RoleCategory.PROCUREMENT),
    (r"supply\s*chain|سلسلة\s*التوريد|لوجستيات|logistics", RoleCategory.SUPPLY_CHAIN),
    (r"operations|إنتاج|انتاج|production|plant", RoleCategory.OPERATIONS),
    (r"owner|general\s+manager|ceo|مدير\s+عام|مالك|رئيس", RoleCategory.OWNER_GM),
    (r"finance|محاسب|accounting|مالي", RoleCategory.FINANCE),
]

ROLE_PRIORITY = {
    RoleCategory.PROCUREMENT: 0,
    RoleCategory.PURCHASING: 1,
    RoleCategory.SUPPLY_CHAIN: 2,
    RoleCategory.OWNER_GM: 3,
    RoleCategory.OPERATIONS: 4,
    RoleCategory.FINANCE: 5,
    RoleCategory.GENERAL: 6,
    RoleCategory.UNKNOWN: 7,
}

GENERIC_LOCALS = {"info", "sales", "contact", "hello", "admin", "office", "hr", "jobs", "careers", "support"}
PREFERRED_LOCALS = {"procurement", "purchasing", "purchase", "supply", "supplychain", "buying", "tenders", "sourcing"}


@dataclass
class ExtractedContact:
    email: str
    source_url: str
    role_category: str = RoleCategory.UNKNOWN
    is_role_based: bool = False
    full_name: str = ""
    role_title: str = ""
    confidence: float = 0.5


def classify_role(email: str, title: str = "") -> tuple[str, bool]:
    local = email.partition("@")[0].lower()
    is_role_based = local in GENERIC_LOCALS or any(
        token in local for token in ("procurement", "purchas", "supply", "sales", "info", "contact")
    )
    for pattern, role in ROLE_PATTERNS:
        if re.search(pattern, local):
            return role, is_role_based
    if title:
        lowered = title.lower()
        for pattern, role in TITLE_PATTERNS:
            if re.search(pattern, lowered):
                return role, is_role_based
    if local in GENERIC_LOCALS:
        return RoleCategory.GENERAL, True
    return RoleCategory.UNKNOWN, is_role_based


def extract_from_text(text: str, source_url: str) -> list[ExtractedContact]:
    found: list[ExtractedContact] = []
    seen: set[str] = set()
    for email in extract_emails(text):
        key = email_match_key(email)
        if key in seen:
            continue
        seen.add(key)
        domain = domain_of_email(email)
        local = email.partition("@")[0].lower()
        if local.startswith(("noreply", "no-reply", "donotreply")):
            continue
        role, is_role = classify_role(email)
        found.append(
            ExtractedContact(
                email=email,
                source_url=source_url,
                role_category=role,
                is_role_based=is_role,
                confidence=0.8 if role in (RoleCategory.PROCUREMENT, RoleCategory.PURCHASING) else 0.6,
            )
        )
        del domain
    found.sort(key=lambda c: (ROLE_PRIORITY.get(RoleCategory(c.role_category), 9), c.email))
    return found


async def find_by_email(session: AsyncSession, email: str) -> Contact | None:
    digest = blind_index(email_match_key(email), purpose="email")
    result = await session.execute(select(Contact).where(Contact.email_hash == digest))
    return result.scalars().first()


async def create_contact(
    session: AsyncSession,
    *,
    company_id: str,
    email: str | None = None,
    phone: str | None = None,
    full_name: str | None = None,
    role_title: str | None = None,
    role_category: str | None = None,
    source_url: str | None = None,
    phone_source_url: str | None = None,
    email_status: str = EmailStatus.UNVERIFIED,
    preferred_language: str | None = None,
    confidence: float = 0.5,
    consent_source: str | None = None,
    is_primary: bool = False,
) -> Contact:
    if not email and not phone:
        raise ValidationFailed("contact needs an email or a phone")
    if email:
        existing = await find_by_email(session, email)
        if existing is not None:
            return existing
    role = role_category or (classify_role(email or "", role_title or "")[0] if email else RoleCategory.UNKNOWN)
    contact = Contact(
        company_id=company_id,
        full_name=full_name or None,
        role_title=role_title or None,
        role_category=str(role),
        email_enc=encrypt_text(email) if email else None,
        email_hash=blind_index(email_match_key(email), purpose="email") if email else None,
        email_is_role_based=classify_role(email or "", role_title or "")[1] if email else False,
        email_status=email_status,
        email_source_url=source_url,
        phone_enc=encrypt_text(normalise_phone(phone)) if phone else None,
        phone_hash=blind_index(normalise_phone(phone), purpose="phone") if phone else None,
        phone_type="unknown",
        phone_source_url=phone_source_url,
        preferred_language=preferred_language or "ar",
        confidence=confidence,
        is_primary=is_primary,
        consent_source=consent_source,
    )
    session.add(contact)
    await session.flush()
    await audit.record(
        session,
        action="contact_created",
        entity_type="contact",
        entity_id=contact.id,
        detail={"company_id": company_id, "email": mask_email(email or ""), "role": str(role)},
    )
    return contact


def contact_email(contact: Contact | None) -> str:
    if contact is None:
        return ""
    return decrypt_text(contact.email_enc)


def contact_phone(contact: Contact | None) -> str:
    if contact is None:
        return ""
    return decrypt_text(contact.phone_enc)


def preferred_language_for(email: str | None, fallback: str = "ar") -> str:
    del email
    return fallback


async def pick_primary(session: AsyncSession, company_id: str) -> Contact | None:
    result = await session.execute(select(Contact).where(Contact.company_id == company_id))
    contacts = list(result.scalars().all())
    if not contacts:
        return None
    usable = [c for c in contacts if c.email_status not in (EmailStatus.INVALID, EmailStatus.DISPOSABLE) and c.email_hash]
    pool = usable or contacts
    pool.sort(
        key=lambda c: (
            0 if c.is_primary else 1,
            ROLE_PRIORITY.get(RoleCategory(c.role_category), 9),
            -c.confidence,
        )
    )
    best = pool[0]
    for candidate in pool:
        if candidate.is_primary and candidate.id != best.id:
            break
    best.is_primary = True
    await session.flush()
    return best


async def list_contacts(session: AsyncSession, company_id: str) -> list[Contact]:
    result = await session.execute(select(Contact).where(Contact.company_id == company_id))
    return list(result.scalars().all())


def public_view(contact: Contact) -> dict[str, object]:
    email = contact_email(contact)
    return {
        "id": contact.id,
        "company_id": contact.company_id,
        "full_name": contact.full_name,
        "role_title": contact.role_title,
        "role_category": contact.role_category,
        "email_masked": mask_email(email),
        "email_status": contact.email_status,
        "email_is_role_based": contact.email_is_role_based,
        "email_source_url": contact.email_source_url,
        "phone_masked": f"***{contact_phone(contact)[-4:]}" if contact.phone_enc else "",
        "phone_source_url": contact.phone_source_url,
        "preferred_language": contact.preferred_language,
        "confidence": contact.confidence,
        "is_primary": contact.is_primary,
        "language_guess": detect_language(contact.full_name or ""),
    }
