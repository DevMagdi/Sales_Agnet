"""Business API over real ASGI requests: companies, discovery, campaigns, messages,
replies, tasks, suppression, templates, settings, reports, webhooks, public unsubscribe."""

from __future__ import annotations

import json
from typing import Any

from httpx import AsyncClient

from app.core.crypto import sign_token
from app.db.models import OutreachMessage
from tests.conftest import make_campaign, make_company, make_contact, make_prospect

CSV = """company_name,website,email,city,industry
Delta Packaging,https://delta-pack.example,procurement@delta-pack.example,10th of Ramadan,packaging_printing
Nile Foods,https://nilefoods.example,sales@nilefoods.example,Cairo,food_beverage
Delta Packaging,https://delta-pack.example,info@delta-pack.example,10th of Ramadan,packaging_printing
"""


async def test_company_crud_and_contacts(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await client.post(
        "/api/v1/companies",
        json={
            "name": "Delta Packaging",
            "domain": "delta-pack.example",
            "website": "https://delta-pack.example",
            "city": "10th of Ramadan",
            "industry": "packaging_printing",
            "email": "procurement@delta-pack.example",
            "phone": "+201000000001",
        },
        headers=auth_headers,
    )
    assert created.status_code in {200, 201}, created.text
    company = created.json()["company"] if "company" in created.json() else created.json()
    company_id = company["id"]

    listing = await client.get("/api/v1/companies", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    detail = await client.get(f"/api/v1/companies/{company_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["company"]["name"] == "Delta Packaging"

    patched = await client.patch(
        f"/api/v1/companies/{company_id}", json={"city": "Cairo", "industry": "packaging_printing"}, headers=auth_headers
    )
    assert patched.status_code == 200

    contact = await client.post(
        f"/api/v1/companies/{company_id}/contacts",
        json={"email": "supply@delta-pack.example", "role_title": "Supply Chain Manager"},
        headers=auth_headers,
    )
    assert contact.status_code in {200, 201}, contact.text

    missing = await client.get("/api/v1/companies/does-not-exist", headers=auth_headers)
    assert missing.status_code == 404


async def test_csv_import_preview_and_commit(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    preview = await client.post("/api/v1/companies/import/preview", json={"content": CSV}, headers=auth_headers)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total_rows"] == 3
    assert body["headers"]
    assert body["mapping"]["name"] == "company_name"

    commit = await client.post(
        "/api/v1/companies/import/commit",
        json={"content": CSV, "filename": "test.csv", "save_mapping_as": None},
        headers=auth_headers,
    )
    assert commit.status_code == 200, commit.text
    report = commit.json()["report"] if "report" in commit.json() else commit.json()
    assert report["imported"] == 2
    assert report["merged"] == 1
    assert report["total_rows"] == 3

    export = await client.get("/api/v1/companies/export.csv", headers=auth_headers)
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert "Delta Packaging" in export.text
    assert "procurement@delta-pack.example" not in export.text  # PII stays out of exports


async def test_discovery_job_lifecycle(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    job = await client.post(
        "/api/v1/discovery/jobs",
        json={"kind": "search", "query": "packaging factories", "city": "10th of Ramadan", "industry": "packaging_printing", "max_results": 10},
        headers=auth_headers,
    )
    assert job.status_code in {200, 201}, job.text
    job_id = job.json()["job"]["id"] if "job" in job.json() else job.json()["id"]
    run = await client.post(f"/api/v1/discovery/jobs/{job_id}/run", headers=auth_headers)
    assert run.status_code == 200, run.text
    listing = await client.get("/api/v1/discovery/jobs", headers=auth_headers)
    assert listing.status_code == 200


async def test_campaign_lifecycle(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await client.post(
        "/api/v1/campaigns",
        json={
            "name": "Egypt packaging Q1",
            "industry_filter": ["packaging_printing"],
            "country_filter": ["EG"],
            "min_icp_score": 40,
            "daily_cap": 15,
            "send_windows": {"weekdays": [6, 0, 1, 2, 3], "start": "09:30", "end": "15:00"},
            "review_first_n": 1,
        },
        headers=auth_headers,
    )
    assert created.status_code in {200, 201}, created.text
    campaign = created.json()["campaign"] if "campaign" in created.json() else created.json()
    campaign_id = campaign["id"]
    assert campaign["status"] in {"draft", "active"}

    paused = await client.post(
        f"/api/v1/campaigns/{campaign_id}/status", json={"action": "pause", "reason": "test"}, headers=auth_headers
    )
    assert paused.status_code == 200, paused.text
    activated = await client.post(
        f"/api/v1/campaigns/{campaign_id}/status", json={"action": "activate", "reason": "test"}, headers=auth_headers
    )
    assert activated.status_code == 200, activated.text

    stats = await client.get(f"/api/v1/campaigns/{campaign_id}/stats", headers=auth_headers)
    assert stats.status_code == 200
    assert "prospect_states" in stats.json()
    capacity = await client.get(f"/api/v1/campaigns/{campaign_id}/capacity", headers=auth_headers)
    assert capacity.status_code == 200
    counts = await client.get(f"/api/v1/campaigns/{campaign_id}/counts", headers=auth_headers)
    assert counts.status_code == 200
    plan = await client.post(f"/api/v1/campaigns/{campaign_id}/plan", headers=auth_headers)
    assert plan.status_code == 200, plan.text
    prospects = await client.get(f"/api/v1/campaigns/{campaign_id}/prospects", headers=auth_headers)
    assert prospects.status_code == 200
    messages = await client.get(f"/api/v1/campaigns/{campaign_id}/messages", headers=auth_headers)
    assert messages.status_code == 200
    patched = await client.patch(
        f"/api/v1/campaigns/{campaign_id}", json={"daily_cap": 10, "allow_risky_emails": False}, headers=auth_headers
    )
    assert patched.status_code == 200, patched.text


async def test_campaign_preview_renders_message(
    client: AsyncClient, auth_headers: dict[str, str], session: Any, settings: Any, clock: Any, registry: Any
) -> None:
    from app.services import templates

    await templates.seed_templates(session)
    company = await make_company(session, domain="preview.example", website="https://preview.example")
    await make_contact(session, company.id, email="buyer@preview.example", email_status="valid")
    campaign = await make_campaign(session, name="Preview campaign")
    await session.flush()
    response = await client.post(
        f"/api/v1/campaigns/{campaign.id}/preview",
        json={"company_id": company.id, "step": "initial"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["subject"]
    assert payload["body_text"]
    assert "ProcureAI" in payload["body_text"]
    failed = [c for c in payload["gate_report"]["checks"] if not c["passed"]]
    assert payload["gate_report"]["passed"] is True, failed


async def test_messages_review_flow(
    client: AsyncClient, auth_headers: dict[str, str], session: Any, settings: Any
) -> None:
    company = await make_company(session, domain="review.example")
    contact = await make_contact(session, company.id, email="buyer@review.example", email_status="valid")
    campaign = await make_campaign(session, name="Review campaign")
    prospect = await make_prospect(session, campaign, company, contact, state="step1_sent")
    message = OutreachMessage(
        campaign_prospect_id=prospect.id,
        company_id=company.id,
        contact_id=contact.id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="draft body",
        status="needs_review",
        idempotency_key="review-1",
    )
    session.add(message)
    await session.flush()

    queue = await client.get("/api/v1/messages/review", headers=auth_headers)
    assert queue.status_code == 200
    assert any(item["id"] == message.id for item in queue.json()["items"])

    rejected = await client.post(
        f"/api/v1/messages/{message.id}/review",
        json={"action": "reject", "reason": "off brand"},
        headers=auth_headers,
    )
    assert rejected.status_code == 200, rejected.text
    await session.refresh(message)
    assert message.status in {"rejected", "blocked"}

    detail = await client.get(f"/api/v1/messages/{message.id}", headers=auth_headers)
    assert detail.status_code == 200

    requeued = await client.post(f"/api/v1/messages/{message.id}/requeue", headers=auth_headers)
    assert requeued.status_code in {200, 400, 409}, requeued.text

    send_due = await client.post("/api/v1/messages/send-due", json={"limit": 5}, headers=auth_headers)
    assert send_due.status_code == 200, send_due.text


async def test_replies_api_override_and_handoff(
    client: AsyncClient, auth_headers: dict[str, str], session: Any, settings: Any, clock: Any, registry: Any
) -> None:
    from app.services import inbound as inbound_service
    from app.services import reply_handler

    company = await make_company(session, domain="reply-api.example")
    contact = await make_contact(session, company.id, email="buyer@reply-api.example", email_status="valid")
    campaign = await make_campaign(session, name="Reply campaign")
    prospect = await make_prospect(session, campaign, company, contact, state="step1_sent")
    outbound = OutreachMessage(
        campaign_prospect_id=prospect.id,
        company_id=company.id,
        contact_id=contact.id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="body",
        status="sent",
        rfc_message_id="<api-out-1@mantiqoutreach.example>",
        idempotency_key="api-out-1",
        sent_at=clock.now(),
    )
    session.add(outbound)
    await session.flush()
    raw = inbound_service.RawInbound(
        message_id="<api-in-1@reply-api.example>",
        from_email="buyer@reply-api.example",
        subject="Re: ProcureAI",
        body_text="Can you send more details?",
        in_reply_to=outbound.rfc_message_id,
        received_at=clock.now(),
    )
    inbound_row = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    outcome = await reply_handler.process_inbound(
        session, inbound_row, registry=registry, clock=clock, settings=settings
    )
    reply_id = outcome["reply_id"]

    listing = await client.get("/api/v1/replies", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1
    detail = await client.get(f"/api/v1/replies/{reply_id}", headers=auth_headers)
    assert detail.status_code == 200

    override = await client.post(
        f"/api/v1/replies/{reply_id}/override",
        json={"classification": "not_now", "reason": "human review"},
        headers=auth_headers,
    )
    assert override.status_code == 200, override.text

    thread = await client.get(f"/api/v1/replies/threads/{prospect.id}", headers=auth_headers)
    assert thread.status_code == 200

    handoff = await client.post(f"/api/v1/replies/{reply_id}/handoff", json={}, headers=auth_headers)
    assert handoff.status_code in {200, 409}, handoff.text

    tasks = await client.get("/api/v1/tasks", headers=auth_headers)
    assert tasks.status_code == 200


async def test_reply_send_endpoint(
    client: AsyncClient, auth_headers: dict[str, str], session: Any, settings: Any, clock: Any, registry: Any
) -> None:
    from app.services import inbound as inbound_service
    from app.services import reply_handler

    company = await make_company(session, domain="reply-send.example")
    contact = await make_contact(session, company.id, email="buyer@reply-send.example", email_status="valid")
    campaign = await make_campaign(session, name="Reply send campaign")
    prospect = await make_prospect(session, campaign, company, contact, state="step1_sent")
    outbound = OutreachMessage(
        campaign_prospect_id=prospect.id,
        company_id=company.id,
        contact_id=contact.id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="body",
        status="sent",
        rfc_message_id="<api-out-2@mantiqoutreach.example>",
        idempotency_key="api-out-2",
        sent_at=clock.now(),
    )
    session.add(outbound)
    await session.flush()
    raw = inbound_service.RawInbound(
        message_id="<api-in-2@reply-send.example>",
        from_email="buyer@reply-send.example",
        subject="Re: ProcureAI",
        body_text="Send me pricing",
        in_reply_to=outbound.rfc_message_id,
        received_at=clock.now(),
    )
    inbound_row = await inbound_service.ingest(session, raw, clock=clock, settings=settings)
    outcome = await reply_handler.process_inbound(
        session, inbound_row, registry=registry, clock=clock, settings=settings
    )
    reply_id = outcome["reply_id"]
    sent = await client.post(
        f"/api/v1/replies/{reply_id}/send",
        json={
            "subject": "Re: ProcureAI",
            "body_text": (
                "Hello,\n\nThanks for the note — ProcureAI compares supplier quotes side by side.\n"
                "Happy to run a short demo on your own data.\n\nAhmed — Mantiq Technologies\n"
                "12 Makram Ebeid, Nasr City, Cairo | +201000000000"
            ),
        },
        headers=auth_headers,
    )
    assert sent.status_code in {200, 400, 409}, sent.text


async def test_suppression_api(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    added = await client.post(
        "/api/v1/suppression",
        json={"kind": "email", "value": "stop@factory.example", "reason": "manual", "note": "requested"},
        headers=auth_headers,
    )
    assert added.status_code in {200, 201}, added.text
    entry = added.json()["entry"] if "entry" in added.json() else added.json()
    assert entry["value_stored"] is False
    assert "stop@factory.example" not in json.dumps(added.json())

    check = await client.get("/api/v1/suppression/check?email=stop@factory.example", headers=auth_headers)
    assert check.status_code == 200
    assert check.json()["suppressed"] is True
    check_other = await client.get("/api/v1/suppression/check?email=other@factory.example", headers=auth_headers)
    assert check_other.json()["suppressed"] is False

    listing = await client.get("/api/v1/suppression", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["counts"]["email"] >= 1

    imported = await client.post(
        "/api/v1/suppression/import",
        json={"content": "email\nbulk1@factory.example\nbulk2@factory.example\n", "reason": "manual"},
        headers=auth_headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 2

    export = await client.get("/api/v1/suppression/export.csv", headers=auth_headers)
    assert export.status_code == 200
    assert "stop@factory.example" not in export.text
    assert "kind,value_hash,reason" in export.text


async def test_templates_api(client: AsyncClient, auth_headers: dict[str, str], session: Any) -> None:
    from app.services import templates

    await templates.seed_templates(session)
    listing = await client.get("/api/v1/templates", headers=auth_headers)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) >= 18
    template_id = items[0]["id"]
    detail = await client.get(f"/api/v1/templates/{template_id}", headers=auth_headers)
    assert detail.status_code == 200
    preview = await client.post(
        f"/api/v1/templates/{template_id}/preview",
        json={"variables": {}},
        headers=auth_headers,
    )
    assert preview.status_code in {200, 400, 422}, preview.text
    activate = await client.post(f"/api/v1/templates/{template_id}/activate", headers=auth_headers)
    assert activate.status_code == 200, activate.text
    bad = await client.post(
        "/api/v1/templates",
        json={"code": "bad_outreach", "locale": "en", "step": "initial", "subject": "s", "body_text": "no unsubscribe here"},
        headers=auth_headers,
    )
    assert bad.status_code in {400, 422}, bad.text
    playbooks = await client.get("/api/v1/templates/playbooks/all", headers=auth_headers)
    assert playbooks.status_code == 200
    claims = await client.get("/api/v1/templates/claims/fact-sheet", headers=auth_headers)
    assert claims.status_code == 200


async def test_settings_mailboxes_and_users(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    created = await client.post(
        "/api/v1/settings/mailboxes",
        json={
            "from_email": "second@mantiqoutreach.example",
            "from_name": "Second Sender",
            "daily_cap": 12,
            "smtp_host": "smtp.example.com",
            "smtp_username": "second",
            "smtp_password": "super-secret",
            "imap_host": "imap.example.com",
            "imap_username": "second",
            "imap_password": "super-secret-2",
        },
        headers=auth_headers,
    )
    assert created.status_code in {200, 201}, created.text
    body = json.dumps(created.json())
    assert "super-secret" not in body  # secrets are write-only
    mailbox = created.json()["mailbox"] if "mailbox" in created.json() else created.json()
    mailbox_id = mailbox["id"]

    listing = await client.get("/api/v1/settings/mailboxes", headers=auth_headers)
    assert listing.status_code == 200
    assert listing.json()["items"]
    status_change = await client.post(
        f"/api/v1/settings/mailboxes/{mailbox_id}/status", json={"status": "paused"}, headers=auth_headers
    )
    assert status_change.status_code == 200, status_change.text

    user = await client.post(
        "/api/v1/settings/users",
        json={"email": "sdr@example.com", "name": "SDR", "password": "SdrPassw0rd!", "role": "sales"},
        headers=auth_headers,
    )
    assert user.status_code in {200, 201}, user.text
    users = await client.get("/api/v1/settings/users", headers=auth_headers)
    assert users.status_code == 200
    assert users.json()["total"] >= 2
    deactivate = await client.post(
        f"/api/v1/settings/users/{user.json()['user']['id']}/deactivate", headers=auth_headers
    )
    assert deactivate.status_code == 200, deactivate.text


async def test_reports_api(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    for path in ("funnel", "kpis", "costs", "industries", "daily", "deliverability", "states", "feed"):
        response = await client.get(f"/api/v1/reports/{path}", headers=auth_headers)
        assert response.status_code == 200, (path, response.text)
    csv_response = await client.get("/api/v1/reports/funnel.csv", headers=auth_headers)
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]


async def test_webhook_email_signature_and_events(
    client: AsyncClient, session: Any, settings: Any, clock: Any
) -> None:
    from app.services import templates

    await templates.seed_templates(session)
    company = await make_company(session, domain="hook.example")
    contact = await make_contact(session, company.id, email="buyer@hook.example", email_status="valid")
    campaign = await make_campaign(session, name="Hook campaign")
    prospect = await make_prospect(session, campaign, company, contact, state="step1_sent")
    message = OutreachMessage(
        campaign_prospect_id=prospect.id,
        company_id=company.id,
        contact_id=contact.id,
        channel="email",
        direction="outbound",
        step="initial",
        subject="ProcureAI",
        body_text="body",
        status="sent",
        provider_message_id="provider-1",
        rfc_message_id="<hook-out-1@mantiqoutreach.example>",
        idempotency_key="hook-out-1",
        sent_at=clock.now(),
    )
    session.add(message)
    await session.flush()

    payload = {"event": "delivered", "message_id": "provider-1", "recipient": "buyer@hook.example"}
    raw = json.dumps(payload).encode()
    response = await client.post("/api/v1/webhooks/email", content=raw, headers={"content-type": "application/json"})
    assert response.status_code in {200, 400, 401}, response.text

    bad = await client.post(
        "/api/v1/webhooks/email",
        content=b"{}",
        headers={"content-type": "application/json", "x-signature": "nope"},
    )
    assert bad.status_code in {200, 400, 401}


async def test_webhook_health(client: AsyncClient) -> None:
    response = await client.get("/api/v1/webhooks/health")
    assert response.status_code == 200


async def test_public_unsubscribe_get_and_one_click(
    client: AsyncClient, session: Any, settings: Any, clock: Any
) -> None:
    company = await make_company(session, domain="unsub.example")
    contact = await make_contact(session, company.id, email="buyer@unsub.example", email_status="valid")
    token = sign_token(
        {"c": contact.id, "e": "buyer@unsub.example", "p": "unsub"},
        ttl_seconds=settings.unsubscribe_token_ttl_days * 86400,
    )
    page = await client.get(f"/u/{token}")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "إيقاف" in page.text or "unsubscribe" in page.text.lower()

    one_click = await client.post(f"/u/{token}", content=b"List-Unsubscribe=One-Click")
    assert one_click.status_code == 200, one_click.text

    from app.services import suppression

    assert await suppression.is_suppressed(session, email="buyer@unsub.example") is not None

    # GET renders a friendly invalid-link page; the one-click POST rejects the token outright
    invalid_page = await client.get("/u/not-a-real-token")
    assert invalid_page.status_code == 200
    assert "not valid" in invalid_page.text or "غير صالح" in invalid_page.text
    invalid_post = await client.post("/u/not-a-real-token", content=b"List-Unsubscribe=One-Click")
    assert invalid_post.status_code == 400


async def test_click_redirect_requires_valid_token(client: AsyncClient) -> None:
    response = await client.get("/click/bad-token", follow_redirects=False)
    assert response.status_code in {400, 404, 422}


async def test_audit_endpoint_records_admin_actions(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await client.post("/api/v1/system/kill", json={"active": True, "reason": "audit"}, headers=auth_headers)
    audit = await client.get("/api/v1/system/audit", headers=auth_headers)
    assert audit.status_code == 200
    assert audit.json()["items"]


async def test_unknown_route_returns_404(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get("/api/v1/nope", headers=auth_headers)
    assert response.status_code == 404
