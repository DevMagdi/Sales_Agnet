"""Idempotent bootstrap: playbooks, templates, admin user, demo data.

`seed_demo.py` uses only fake `.example` domains and fictional companies — never real businesses.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import ValidationFailed
from app.core.security import hash_password
from app.core.text import normalise_email
from app.core.yamlconf import load_industries
from app.db.models import (
    Campaign,
    Company,
    IndustryPlaybookRow,
    Mailbox,
    MailboxPool,
    User,
)
from app.db.models.enums import (
    CampaignStatus,
    CompanyLanguage,
    CompanyStatus,
    SourceKind,
    UserRole,
)
from app.services import contacts as contact_service
from app.services import templates

DEMO_COMPANIES: list[dict[str, Any]] = [
    {
        "name": "Delta Packaging Industries",
        "name_ar": "دلتا لصناعات التعبئة",
        "domain": "delta-packaging.example",
        "city": "10th of Ramadan",
        "industry": "packaging_printing",
        "summary": "Corrugated carton manufacturer serving food and beverage clients.",
        "emails": ["procurement@delta-packaging.example", "info@delta-packaging.example"],
    },
    {
        "name": "Nile Plastics Co.",
        "name_ar": "النيل للبلاستيك",
        "domain": "nileplastics.example",
        "city": "Borg El Arab",
        "industry": "plastics_rubber",
        "summary": "Injection moulding plant producing containers for dairy and detergents.",
        "emails": ["purchasing@nileplastics.example"],
    },
    {
        "name": "Suez Steel Works",
        "name_ar": "سويس للصلب",
        "domain": "suezsteel.example",
        "city": "Suez/Sokhna",
        "industry": "building_materials_steel",
        "summary": "Steel rebar producer with two rolling lines.",
        "emails": ["supply@suezsteel.example", "sales@suezsteel.example"],
    },
    {
        "name": "Cairo Textile Mills",
        "name_ar": "مطاحن القاهرة للنسيج",
        "domain": "cairotextile.example",
        "city": "Alexandria",
        "industry": "textiles_garments",
        "summary": "Cotton yarn spinner and fabric supplier for garment exporters.",
        "emails": ["purchasing@cairotextile.example"],
    },
    {
        "name": "Heliopolis Chemicals",
        "name_ar": "هليوبوليس للكيماويات",
        "domain": "heliopolis-chem.example",
        "city": "6th of October",
        "industry": "chemicals_paints_detergents",
        "summary": "Detergent and paint input distributor with laboratory support.",
        "emails": ["buying@heliopolis-chem.example"],
    },
]


async def seed_playbooks(session: AsyncSession, path: str | None = None) -> int:
    industries = load_industries(path)
    created = 0
    for playbook in industries.industries:
        existing = await session.execute(
            select(IndustryPlaybookRow).where(IndustryPlaybookRow.code == playbook.code)
        )
        row = existing.scalars().first()
        if row is None:
            session.add(
                IndustryPlaybookRow(
                    code=playbook.code,
                    name_en=playbook.name_en,
                    name_ar=playbook.name_ar,
                    purchased_categories=playbook.purchased_categories,
                    pain_hypotheses=playbook.pain_hypotheses,
                    demo_scenario=playbook.demo_scenario,
                    example_rfq_items=playbook.example_rfq_items,
                    typical_zones=playbook.typical_zones,
                    regulated=playbook.regulated,
                )
            )
            created += 1
        else:
            row.name_en = playbook.name_en
            row.name_ar = playbook.name_ar
            row.purchased_categories = playbook.purchased_categories
            row.pain_hypotheses = playbook.pain_hypotheses
            row.demo_scenario = playbook.demo_scenario
            row.example_rfq_items = playbook.example_rfq_items
            row.typical_zones = playbook.typical_zones
            row.regulated = playbook.regulated
    await session.flush()
    return created


async def ensure_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    role: str = UserRole.ADMIN,
    name: str = "User",
    locale: str = "en",
) -> User:
    normalised = normalise_email(email)
    existing = await session.execute(select(User).where(User.email == normalised))
    user = existing.scalars().first()
    if user is not None:
        return user
    if str(role) not in {str(r) for r in UserRole}:
        raise ValidationFailed(f"unknown role: {role}")
    user = User(
        email=normalised,
        name=name,
        password_hash=hash_password(password),
        role=str(role),
        locale=locale,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def ensure_admin(
    session: AsyncSession, *, email: str, password: str, name: str = "Administrator"
) -> User:
    return await ensure_user(
        session, email=email, password=password, role=UserRole.ADMIN, name=name
    )


async def ensure_mailbox(session: AsyncSession, settings: Settings, clock: Clock) -> Mailbox | None:
    if not settings.sender_email:
        return None
    existing = await session.execute(select(Mailbox).where(Mailbox.from_email == settings.sender_email))
    mailbox = existing.scalars().first()
    if mailbox is not None:
        return mailbox
    pool = (await session.execute(select(MailboxPool).where(MailboxPool.name == "default"))).scalars().first()
    if pool is None:
        pool = MailboxPool(name="default", description="Default sending pool")
        session.add(pool)
        await session.flush()
    from app.services import mailboxes as mailbox_service

    return await mailbox_service.create(
        session,
        from_email=settings.sender_email,
        from_name=settings.sender_name or "Mantiq",
        pool_id=pool.id,
        daily_cap=20,
        smtp_host=settings.smtp_host or None,
        smtp_port=settings.smtp_port,
        smtp_username=settings.smtp_username or None,
        smtp_password=settings.smtp_password or None,
        imap_host=settings.imap_host or None,
        imap_port=settings.imap_port,
        imap_username=settings.imap_username or None,
        imap_password=settings.imap_password or None,
        signature_text="",
        physical_address=settings.company_address,
        phone=settings.company_phone,
        provider="smtp" if settings.smtp_host else "fake",
    )


async def seed_demo_companies(session: AsyncSession, clock: Clock) -> list[Company]:
    created: list[Company] = []
    for spec in DEMO_COMPANIES:
        existing = await session.execute(select(Company).where(Company.domain == spec["domain"]))
        if existing.scalars().first() is not None:
            continue
        company = Company(
            name=spec["name"],
            name_ar=spec["name_ar"],
            domain=spec["domain"],
            website=f"https://{spec['domain']}",
            country="EG",
            city=spec["city"],
            industry=spec["industry"],
            source=SourceKind.MANUAL,
            source_ref="seed_demo",
            status=CompanyStatus.RESEARCHED,
            language=CompanyLanguage.AR,
            size_hint="medium",
            icp_score=72,
            icp_reasons=[{"reason": "demo_seed", "points": 72, "detail": spec["summary"]}],
        )
        session.add(company)
        await session.flush()
        for email in spec["emails"]:
            await contact_service.create_contact(
                session,
                company_id=company.id,
                email=email,
                role_category=contact_service.classify_role(email)[0],
                source_url=f"https://{spec['domain']}/contact",
                email_status="valid",
                preferred_language="ar",
                confidence=0.8,
            )
        created.append(company)
    await session.flush()
    return created


async def seed_campaign(session: AsyncSession, settings: Settings, clock: Clock) -> Campaign:
    existing = await session.execute(select(Campaign).where(Campaign.name == "Egypt — Packaging pilot"))
    campaign = existing.scalars().first()
    if campaign is not None:
        return campaign
    pool = (await session.execute(select(MailboxPool).where(MailboxPool.name == "default"))).scalars().first()
    campaign = Campaign(
        name="Egypt — Packaging pilot",
        status=CampaignStatus.DRAFT,
        industry_filter=["packaging_printing"],
        country_filter=["EG"],
        min_icp_score=60,
        language_policy="auto",
        template_set="default",
        mailbox_pool_id=pool.id if pool else None,
        daily_cap=20,
        send_windows={"weekdays": [6, 0, 1, 2, 3], "start": "09:30", "end": "15:00"},
        timezone="Africa/Cairo",
        review_first_n=0,
        auto_reply_classes=["unsubscribe", "wants_info", "meeting_proposal"],
        channels_enabled=["email"],
        max_followups=2,
        followup_gaps_days=[3, 7],
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def seed_all(
    session: AsyncSession, *, settings: Settings, clock: Clock, admin_email: str = "", admin_password: str = ""
) -> dict[str, Any]:
    playbooks = await seed_playbooks(session)
    template_result = await templates.seed_templates(session)
    admin = None
    if admin_email and admin_password:
        admin = await ensure_admin(session, email=admin_email, password=admin_password)
    mailbox = await ensure_mailbox(session, settings, clock)
    return {
        "playbooks_created": playbooks,
        "templates": template_result,
        "admin": admin.email if admin else None,
        "mailbox": mailbox.from_email if mailbox else None,
    }
