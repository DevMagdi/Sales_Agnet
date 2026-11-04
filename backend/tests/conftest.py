"""Test fixtures: in-memory SQLite, deterministic adapters, a seeded org, and an ASGI client."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("OUTREACH_MODE", "dry_run")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-tests-only-0123456789")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "test-master-key-for-tests-only-0123456789")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("FEATURES_URL", "http://testserver/features")
os.environ.setdefault("BOOKING_URL", "http://testserver/book")
os.environ.setdefault("SENDER_NAME", "Ahmed Mantiq")
os.environ.setdefault("SENDER_TITLE", "Partnerships")
os.environ.setdefault("SENDER_EMAIL", "ahmed@mantiqoutreach.example")
os.environ.setdefault("COMPANY_ADDRESS", "12 Makram Ebeid, Nasr City, Cairo, Egypt")
os.environ.setdefault("COMPANY_PHONE", "+201000000000")
os.environ.setdefault("POC_OFFER_TEXT_AR", "لو تحبوا، بنعمل Demo {duration} دقيقة على بياناتكم.")
os.environ.setdefault("POC_OFFER_TEXT_EN", "If useful, we can run a {duration}-minute demo on your data.")
os.environ.setdefault("DEMO_DURATION_MINUTES", "20")
os.environ.setdefault("CRAWL_ALLOW_LOCAL_HOSTS", "true")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMAIL_PROVIDER", "fake")

import app.db.models  # noqa: F401,E402
from app.adapters.calendar.link import FakeCalendarAdapter  # noqa: E402
from app.adapters.email.fake import FakeEmailAdapter  # noqa: E402
from app.adapters.llm.fake import DeterministicLLM  # noqa: E402
from app.adapters.notify.fake import FakeNotify  # noqa: E402
from app.adapters.places.fake import FakePlaces  # noqa: E402
from app.adapters.registry import AdapterRegistry, set_registry  # noqa: E402
from app.adapters.search.fake import FakeSearch  # noqa: E402
from app.adapters.verify.fake import FakeVerify  # noqa: E402
from app.adapters.whatsapp.fake import FakeWhatsAppAdapter  # noqa: E402
from app.core.clock import FrozenClock  # noqa: E402
from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import configure_engine  # noqa: E402
from app.services import seed, templates  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _settings_env() -> Any:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Any:
    return get_settings()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 1, 6, 8, 0, tzinfo=UTC))  # Tuesday 10:00 Cairo


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine: Any) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    configure_engine(engine)
    async with factory() as sess:
        yield sess
        await sess.commit()


@pytest.fixture
def llm() -> DeterministicLLM:
    return DeterministicLLM()


@pytest.fixture
def email_adapter() -> FakeEmailAdapter:
    return FakeEmailAdapter()


@pytest.fixture
def registry(llm: DeterministicLLM, email_adapter: FakeEmailAdapter, settings: Any) -> AdapterRegistry:
    reg = AdapterRegistry(
        llm=llm,
        email=email_adapter,
        whatsapp=FakeWhatsAppAdapter(),
        search=FakeSearch(),
        places=FakePlaces(),
        verify=FakeVerify(),
        calendar=FakeCalendarAdapter(settings.booking_url),
        notify=FakeNotify(),
    )
    set_registry(reg)
    return reg


@pytest_asyncio.fixture
async def seeded(session: AsyncSession, settings: Any, clock: FrozenClock) -> dict[str, Any]:
    result = await seed.seed_all(
        session, settings=settings, clock=clock, admin_email="admin@example.com", admin_password="AdminPassw0rd!"
    )
    await session.flush()
    return result


@pytest_asyncio.fixture
async def app(session: AsyncSession) -> AsyncIterator[Any]:
    from app.main import create_app

    application = create_app()
    yield application


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    application = create_app()
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def auth_headers(session: AsyncSession, seeded: dict[str, Any], client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": "AdminPassw0rd!"}
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def make_company(session: AsyncSession, **overrides: Any) -> Any:
    from app.core.ids import new_id
    from app.db.models import Company

    defaults = {
        "name": "Delta Packaging",
        "domain": f"delta-{new_id()[:8]}.example",
        "website": "https://delta.example",
        "country": "EG",
        "city": "10th of Ramadan",
        "industry": "packaging_printing",
        "status": "qualified",
        "icp_score": 80,
        "language": "ar",
        "size_hint": "medium",
    }
    defaults.update(overrides)
    company = Company(**defaults)
    session.add(company)
    await session.flush()
    return company


async def make_contact(session: AsyncSession, company_id: str, **overrides: Any) -> Any:
    from app.services import contacts as contact_service

    defaults = {
        "company_id": company_id,
        "email": f"procurement-{company_id[:6]}@example.com",
        "role_category": "procurement",
        "source_url": "https://delta.example/contact",
        "email_status": "valid",
        "preferred_language": "ar",
    }
    defaults.update(overrides)
    email = defaults.pop("email")
    return await contact_service.create_contact(
        session,
        email=email,
        role_title=defaults.pop("role_title", None),
        **defaults,
    )


async def make_campaign(session: AsyncSession, **overrides: Any) -> Any:
    from app.core.ids import new_id
    from app.db.models import Campaign

    defaults = {
        "name": f"Test campaign {new_id()[:6]}",
        "status": "active",
        "min_icp_score": 50,
        "daily_cap": 20,
        "send_windows": {"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"},
        "timezone": "Africa/Cairo",
        "max_followups": 2,
        "followup_gaps_days": [3, 7],
        "auto_reply_classes": ["unsubscribe", "wants_info", "meeting_proposal"],
        "channels_enabled": ["email"],
    }
    defaults.update(overrides)
    campaign = Campaign(**defaults)
    session.add(campaign)
    await session.flush()
    return campaign


async def make_mailbox(session: AsyncSession, **overrides: Any) -> Any:
    from app.services import mailboxes as mailbox_service

    defaults = {
        "from_email": f"sender{datetime.now(UTC).timestamp()}@mantiqoutreach.example".replace(".", "", 1),
        "from_name": "Ahmed Mantiq",
        "daily_cap": 20,
        "physical_address": "12 Makram Ebeid, Nasr City, Cairo",
        "phone": "+201000000000",
        "provider": "fake",
    }
    defaults.update(overrides)
    return await mailbox_service.create(session, **defaults)


async def make_prospect(session: AsyncSession, campaign: Any, company: Any, contact: Any, **overrides: Any) -> Any:
    from app.db.models import CampaignProspect

    defaults = {
        "campaign_id": campaign.id,
        "company_id": company.id,
        "contact_id": contact.id,
        "state": "queued",
        "step_index": 0,
        "language": "ar",
    }
    defaults.update(overrides)
    prospect = CampaignProspect(**defaults)
    session.add(prospect)
    await session.flush()
    return prospect


@pytest.fixture
def seeded_templates() -> Any:
    return templates
