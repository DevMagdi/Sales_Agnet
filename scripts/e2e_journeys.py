#!/usr/bin/env python3
"""End-to-end journeys against the real application (no mocks beyond the provider adapters).

Runs the FastAPI app in-process with a throwaway SQLite database, a real HTTP client over ASGI,
real auth, the real send gate, the real reply pipeline and the real database writes. Prints one
line per step and exits non-zero if any step fails.

    python scripts/e2e_journeys.py                # dry_run (default)
    E2E_KEEP_DB=1 python scripts/e2e_journeys.py  # keep the sqlite file for inspection
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

DB_PATH = Path(os.environ.get("E2E_DB", Path(tempfile.gettempdir()) / "mantiq_e2e.db"))
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("OUTREACH_MODE", "dry_run")
os.environ.setdefault("SECRET_KEY", "e2e-secret-key-0123456789-0123456789")
os.environ.setdefault("ENCRYPTION_MASTER_KEY", "e2e-master-key-0123456789-0123456789")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("FEATURES_URL", "http://testserver/features")
os.environ.setdefault("BOOKING_URL", "http://testserver/book")
os.environ.setdefault("SENDER_NAME", "Ahmed Mantiq")
os.environ.setdefault("SENDER_TITLE", "Partnerships — Mantiq Technologies")
os.environ.setdefault("SENDER_EMAIL", "ahmed@mantiqoutreach.example")
os.environ.setdefault("COMPANY_ADDRESS", "12 Makram Ebeid, Nasr City, Cairo, Egypt")
os.environ.setdefault("COMPANY_PHONE", "+201000000000")
os.environ.setdefault("POC_OFFER_TEXT_AR", "لو تحبوا نعمل Demo {duration} دقيقة على بياناتكم.")
os.environ.setdefault("POC_OFFER_TEXT_EN", "If useful we can run a {duration}-minute demo on your data.")
os.environ.setdefault("DEMO_DURATION_MINUTES", "20")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMAIL_PROVIDER", "fake")
os.environ.setdefault("SEARCH_PROVIDER", "fake")
os.environ.setdefault("CRAWL_ALLOW_LOCAL_HOSTS", "true")

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "AdminPassw0rd!"

failures: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(f"{name}: {detail}")


def expect(response: Any, name: str, allowed: tuple[int, ...] = (200, 201)) -> Any:
    body = ""
    try:
        payload = response.json()
        body = json.dumps(payload, ensure_ascii=False)[:220]
    except Exception:  # noqa: BLE001 - non-JSON responses are fine for some endpoints
        body = response.text[:220]
    step(name, response.status_code in allowed, f"{response.status_code} {body}")
    return response


async def main() -> int:
    from httpx import ASGITransport, AsyncClient

    from app.adapters.registry import build_registry, set_registry
    from app.core.clock import SystemClock
    from app.core.config import get_settings
    from app.db.session import create_all, dispose_engine, session_scope
    from app.main import create_app
    from app.services import seed

    if DB_PATH.exists() and not os.environ.get("E2E_KEEP_DB"):
        DB_PATH.unlink()

    settings = get_settings()
    set_registry(build_registry(settings))
    await create_all()
    clock = SystemClock()
    async with session_scope() as session:
        await seed.seed_all(
            session, settings=settings, clock=clock, admin_email=ADMIN_EMAIL, admin_password=ADMIN_PASSWORD
        )
        await seed.seed_demo_companies(session, clock)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        print("\nJourney 1 — cold outbound to a hand-off")
        login = expect(await client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}), "login")
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = expect(await client.get("/api/v1/auth/me", headers=headers), "auth /me")
        step("role is admin", me.json()["role"] == "admin", me.json()["role"])

        expect(await client.get("/api/v1/system/status", headers=headers), "system status")
        expect(await client.get("/api/v1/system/doctor", headers=headers), "deliverability doctor")

        campaign = await client.post(
            "/api/v1/campaigns",
            json={
                "name": "E2E Egypt packaging",
                "industry_filter": ["packaging_printing"],
                "country_filter": ["EG"],
                "min_icp_score": 40,
                "daily_cap": 10,
                "send_windows": {"weekdays": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"},
                "review_first_n": 1,
            },
            headers=headers,
        )
        expect(campaign, "create campaign")
        campaign_payload = campaign.json()
        campaign_id = campaign_payload.get("campaign", campaign_payload)["id"]
        expect(
            await client.post(
                f"/api/v1/campaigns/{campaign_id}/status",
                json={"action": "activate", "reason": "e2e"},
                headers=headers,
            ),
            "activate campaign",
        )

        plan = expect(await client.post(f"/api/v1/campaigns/{campaign_id}/plan", headers=headers), "plan sequences")
        step("prospects created", plan.json()["created"] >= 1, json.dumps(plan.json(), ensure_ascii=False))

        compose = expect(
            await client.post("/api/v1/messages/compose-due", json={"limit": 5}, headers=headers),
            "compose due messages",
        )
        step(
            "message composed and held for review",
            compose.json().get("composed", 0) >= 1,
            json.dumps(compose.json(), ensure_ascii=False)[:200],
        )
        dispatch = expect(
            await client.post("/api/v1/messages/send-due", json={"limit": 5}, headers=headers), "send-due"
        )
        step("send-due ran through the gate", "claimed" in dispatch.json(), json.dumps(dispatch.json())[:160])

        queue = expect(await client.get("/api/v1/messages/review", headers=headers), "review queue")
        items = queue.json()["items"]
        step("first message held for review", len(items) >= 1, f"{len(items)} item(s)")

        if items:
            approve = await client.post(
                f"/api/v1/messages/{items[0]['id']}/review",
                json={"action": "approve"},
                headers=headers,
            )
            step(
                "review approval re-runs the gate",
                approve.status_code == 200,
                f"{approve.status_code} {approve.text[:200]}",
            )

        kpis = expect(await client.get("/api/v1/reports/kpis", headers=headers), "KPIs")
        step("kpis present", "sent" in kpis.json(), json.dumps(kpis.json())[:200])
        expect(await client.get("/api/v1/reports/funnel", headers=headers), "funnel")
        expect(await client.get("/api/v1/reports/funnel.csv", headers=headers), "funnel csv")

        prospects = (await client.get(f"/api/v1/campaigns/{campaign_id}/prospects", headers=headers)).json()
        step("campaign has prospects", len(prospects["items"]) >= 1, f"{len(prospects['items'])}")

        print("\nJourney 2 — the prospect replies and a human takes over")
        from app.core.crypto import sign_token
        from app.services import inbound as inbound_service
        from app.services import reply_handler
        from app.adapters.registry import get_registry

        async with session_scope() as session:
            from sqlalchemy import select

            from app.db.models import CampaignProspect, Company, Contact, OutreachMessage

            prospect_row = (
                await session.execute(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign_id))
            ).scalars().first()
            step("prospect exists", prospect_row is not None, "")
            if prospect_row is None:
                return finish()
            company = await session.get(Company, prospect_row.company_id)
            contact = await session.get(Contact, prospect_row.contact_id) if prospect_row.contact_id else None
            outbound = (
                await session.execute(
                    select(OutreachMessage)
                    .where(OutreachMessage.campaign_prospect_id == prospect_row.id)
                    .order_by(OutreachMessage.created_at.desc())
                )
            ).scalars().first()
            # dry_run never hands a message to a provider, so this journey simulates what a real
            # send would have produced: a stored Message-ID the reply can reference.
            if outbound is not None and not outbound.rfc_message_id:
                outbound.rfc_message_id = "<e2e-outbound-1@mantiqoutreach.example>"
                outbound.status = "sent"
                outbound.sent_at = clock.now()
                await session.flush()
            raw = inbound_service.RawInbound(
                message_id="<e2e-reply-1@prospect.example>",
                from_email="procurement@delta-pack.example" if contact is None else None,
                subject="Re: ProcureAI",
                body_text="مهتمين، ممكن نبعت لنا تفاصيل أكتر ونحدد مكالمة الأسبوع الجاي؟",
                in_reply_to=outbound.rfc_message_id if outbound else None,
                received_at=clock.now(),
            )
            if raw.from_email is None and contact is not None:
                from app.services.contacts import contact_email

                raw.from_email = contact_email(contact)
            inbound_row = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
            step("inbound ingested and threaded", inbound_row.campaign_prospect_id == prospect_row.id, str(inbound_row.campaign_prospect_id))
            outcome = await reply_handler.process_inbound(
                session, inbound_row, registry=get_registry(), clock=clock, settings=settings
            )
            step("reply classified", bool(outcome.get("classified")), json.dumps(outcome, ensure_ascii=False)[:220])

        replies = expect(await client.get("/api/v1/replies", headers=headers), "reply list")
        reply_items = replies.json()["items"]
        step("reply visible to staff", len(reply_items) >= 1, f"{len(reply_items)}")
        if reply_items:
            reply_id = reply_items[0]["id"]
            expect(await client.get(f"/api/v1/replies/{reply_id}", headers=headers), "reply detail")
            handoff = await client.post(f"/api/v1/replies/{reply_id}/handoff", json={}, headers=headers)
            step("hand-off created", handoff.status_code in (200, 409), f"{handoff.status_code} {handoff.text[:160]}")
        expect(await client.get("/api/v1/tasks", headers=headers), "tasks list")

        print("\nJourney 3 — one-click unsubscribe is absolute")
        async with session_scope() as session:
            from sqlalchemy import select

            from app.db.models import CampaignProspect, Contact

            prospect_row = (
                await session.execute(select(CampaignProspect).where(CampaignProspect.campaign_id == campaign_id))
            ).scalars().first()
            contact = await session.get(Contact, prospect_row.contact_id) if prospect_row and prospect_row.contact_id else None
            step("contact available", contact is not None, "")
            if contact is None:
                return finish()
            from app.services.contacts import contact_email

            email = contact_email(contact)
            contact_id = contact.id
        unsubscribe_token = sign_token({"c": contact_id, "e": email, "p": "unsub"}, ttl_seconds=86400)
        page = await client.get(f"/u/{unsubscribe_token}")
        step("unsubscribe page renders", page.status_code == 200 and "html" in page.headers.get("content-type", ""), str(page.status_code))
        one_click = await client.post(f"/u/{unsubscribe_token}", content=b"List-Unsubscribe=One-Click")
        step("RFC 8058 one-click accepted", one_click.status_code == 200, str(one_click.status_code))
        from app.services import suppression

        async with session_scope() as session:
            match = await suppression.is_suppressed(session, email=email)
            step("address is suppressed", match is not None, match.kind if match else "not suppressed")
            from app.db.models import CampaignProspect as CP

            row = await session.get(CP, prospect_row.id)
            step("prospect moved to do_not_contact", row.state == "do_not_contact", str(row.state))

        print("\nJourney 4 — kill switch and circuit breakers")
        kill = await client.post("/api/v1/system/kill", json={"active": True, "reason": "e2e"}, headers=headers)
        step("kill switch engages", kill.status_code == 200, str(kill.status_code))
        blocked_send = await client.post("/api/v1/messages/send-due", json={"limit": 2}, headers=headers)
        step(
            "sending is refused while stopped",
            blocked_send.status_code == 200 and blocked_send.json().get("claimed", 0) == 0,
            json.dumps(blocked_send.json())[:160],
        )
        resume = await client.post("/api/v1/system/resume", json={}, headers=headers)
        step("resume clears the stop", resume.status_code == 200, str(resume.status_code))
        breakers = expect(await client.post("/api/v1/system/breakers/evaluate", headers=headers), "breaker evaluation")
        step("breaker verdict returned", "tripped" in breakers.json(), json.dumps(breakers.json())[:160])

        print("\nJourney 5 — public pages and metrics")
        expect(await client.get("/features?lang=ar"), "feature page (ar)")
        expect(await client.get("/features?lang=en"), "feature page (en)")
        expect(await client.get("/bot"), "crawler page")
        metrics = await client.get("/metrics")
        step("prometheus metrics", metrics.status_code == 200 and "adapter_ok" in metrics.text, str(metrics.status_code))

    return finish()


def finish() -> int:
    print()
    if failures:
        print(f"FAILED ({len(failures)} step(s)):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("ALL JOURNEYS PASSED")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        import asyncio as _asyncio

        from app.db.session import dispose_engine

        try:
            _asyncio.get_event_loop().run_until_complete(dispose_engine())
        except Exception:  # noqa: BLE001 - loop already closed
            pass
        if not os.environ.get("E2E_KEEP_DB") and DB_PATH.exists():
            DB_PATH.unlink(missing_ok=True)
    raise SystemExit(code)
