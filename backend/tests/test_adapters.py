"""Adapters: fakes are deterministic, SMTP signs with DKIM, webhooks verify signatures."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import pytest

from app.adapters.base import OutboundEmail
from app.adapters.email import build_email
from app.adapters.email.fake import FakeEmailAdapter
from app.adapters.email.mime import build_mime
from app.adapters.email.smtp import SMTPEmailAdapter
from app.adapters.llm.fake import DeterministicLLM
from app.adapters.places.fake import FakePlaces
from app.adapters.registry import build_registry
from app.adapters.search.fake import FakeSearch
from app.adapters.verify.local import LocalVerifyAdapter
from app.adapters.whatsapp.cloud import WhatsAppCloudAdapter as MetaWhatsAppAdapter
from app.adapters.whatsapp.fake import FakeWhatsAppAdapter
from app.core.config import Settings


@pytest.mark.asyncio
async def test_fake_llm_is_deterministic() -> None:
    llm = DeterministicLLM()
    first = await llm.complete_json(task="qualify", system="s", user="Delta Packaging cartons")
    second = await llm.complete_json(task="qualify", system="s", user="Delta Packaging cartons")
    assert first.text == second.text
    assert first.model.startswith("fake-")
    assert llm.calls
    assert llm.calls[0]["task"] == "qualify"


@pytest.mark.asyncio
async def test_fake_llm_accepts_scripted_json() -> None:
    llm = DeterministicLLM()
    llm.push("classify_reply", {"reply_class": "unsubscribe", "confidence": 0.99})
    result = await llm.complete_json(task="classify_reply", system="s", user="اوقفوا الرسائل")
    assert '"unsubscribe"' in result.text


def _outbound(**overrides: Any) -> OutboundEmail:
    values: dict[str, Any] = {
        "to_email": "a@b.example",
        "from_email": "me@mantiqoutreach.example",
        "from_name": "Ahmed",
        "subject": "ProcureAI",
        "body_text": "body",
        "unsubscribe_url": "https://outreach.mantiqoutreach.example/u/tok",
    }
    values.update(overrides)
    return OutboundEmail(**values)


@pytest.mark.asyncio
async def test_fake_email_writes_to_outbox_and_verifies_events() -> None:
    adapter = FakeEmailAdapter()
    result = await adapter.send(_outbound())
    assert result.ok
    assert result.provider_message_id
    assert len(adapter.sent) == 1
    assert adapter.sent[0].to_email == "a@b.example"
    event = adapter.parse_event({"message_id": result.provider_message_id, "event": "delivered"})
    assert event[0].type == "delivered"


@pytest.mark.asyncio
async def test_fake_email_can_simulate_failures() -> None:
    adapter = FakeEmailAdapter()
    adapter.fail_next = "simulated smtp outage"
    result = await adapter.send(_outbound())
    assert not result.ok
    assert result.error == "simulated smtp outage"


@pytest.mark.asyncio
async def test_search_and_places_are_deterministic() -> None:
    from app.adapters.base import SearchResult

    search = FakeSearch()
    search.add("packaging", [SearchResult(title="Delta", url="https://delta.example", snippet="cartons")])
    a = await search.search("packaging factories in Egypt")
    b = await search.search("packaging factories in Egypt")
    assert [x.url for x in a] == [x.url for x in b] == ["https://delta.example"]
    assert await search.search("nothing matches") == []
    from app.adapters.base import PlaceResult

    places = FakePlaces([PlaceResult(name="Delta", website="https://delta.example", phone="+201000000000", address="Cairo")])
    rows = await places.text_search("packaging")
    assert rows
    assert rows[0].website == "https://delta.example"
    assert await places.text_search("nothing") == rows  # fake is deterministic, not query-dependent


@pytest.mark.asyncio
async def test_local_verify_handles_syntax_blocklists_and_offline_dns() -> None:
    verify = LocalVerifyAdapter()
    offline = await verify.verify("procurement@factory.example")
    assert offline.status == "risky"
    assert offline.reason == "mx_unverified_offline"
    bad = await verify.verify("not-an-email")
    assert bad.status == "invalid"
    assert bad.reason == "syntax"
    disposable = await verify.verify("someone@mailinator.com")
    assert disposable.status == "disposable"
    free_mail = await verify.verify("someone@gmail.com")
    assert free_mail.status == "risky"
    assert free_mail.reason == "free_mail_domain"


@pytest.mark.asyncio
async def test_whatsapp_fake_records_sends() -> None:
    adapter = FakeWhatsAppAdapter()
    result = await adapter.send_text("+201234567890", "hello")
    assert result.ok
    assert adapter.sent[-1]["to"] == "+201234567890"
    template = await adapter.send_template("+201234567890", "demo_followup", "ar", ["Delta"])
    assert template.ok
    assert adapter.sent[-1]["kind"] == "template"


def test_meta_whatsapp_signature() -> None:
    adapter = MetaWhatsAppAdapter(token="t", phone_number_id="1", app_secret="secret")
    body = b'{"entry":[]}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert adapter.verify_signature(body, f"sha256={digest}")
    assert not adapter.verify_signature(body, "sha256=deadbeef")
    assert not adapter.verify_signature(body, "")


def test_whatsapp_webhook_parsing() -> None:
    adapter = MetaWhatsAppAdapter(token="t", phone_number_id="1", app_secret="s")
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "201234567890",
                                    "id": "wamid.1",
                                    "timestamp": "1767600000",
                                    "type": "text",
                                    "text": {"body": "مهتم، عايز أعرف أكتر"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    events = adapter.parse_events(payload)
    assert len(events) == 1
    assert events[0].type == "message"
    assert events[0].from_phone == "201234567890"  # normalised to E.164 by inbound.ingest
    assert "مهتم" in (events[0].text or "")


def test_whatsapp_healthcheck_requires_all_credentials() -> None:
    assert MetaWhatsAppAdapter(token="", phone_number_id="", app_secret="").healthcheck()[0] is False
    assert MetaWhatsAppAdapter(token="t", phone_number_id="1", app_secret="s").healthcheck()[0] is True


def _rsa_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


def test_smtp_adapter_builds_dkim_signature() -> None:
    from app.adapters.email.dkim import sign as dkim_sign

    message = OutboundEmail(
        to_email="a@b.example",
        subject="ProcureAI",
        body_text="hello",
        from_email="me@mantiqoutreach.example",
        from_name="Ahmed",
        unsubscribe_url="https://outreach.mantiqoutreach.example/u/tok",
    )
    raw, _message_id = build_mime(message)
    signed = dkim_sign(
        raw, domain="mantiqoutreach.example", selector="mantiq", private_key=_rsa_key_pem()
    )
    text = signed.decode()
    assert text.startswith("DKIM-Signature: v=1")
    assert "d=mantiqoutreach.example" in text
    assert "s=mantiq" in text
    assert b"List-Unsubscribe: <https://outreach.mantiqoutreach.example/u/tok>" in signed
    assert b"List-Unsubscribe-Post: List-Unsubscribe=One-Click" in signed
    assert b"ProcureAI" in signed


def test_dkim_requires_key_and_selector() -> None:
    from app.adapters.email.dkim import sign as dkim_sign
    from app.core.errors import ConfigError

    raw = b"From: a@b.example\r\nTo: c@d.example\r\nSubject: s\r\n\r\nbody"
    with pytest.raises(ConfigError):
        dkim_sign(raw, domain="", selector="s", private_key=_rsa_key_pem())


def test_smtp_adapter_healthcheck_needs_credentials() -> None:
    adapter = SMTPEmailAdapter(host="", port=587, username="", password="")
    ok, detail = adapter.healthcheck()
    assert not ok
    assert "missing" in detail
    ok, detail = SMTPEmailAdapter(host="smtp.example.com", port=587, username="u", password="p").healthcheck()
    assert ok
    assert "smtp.example.com" in detail


def test_email_provider_selection() -> None:
    assert build_email(Settings(EMAIL_PROVIDER="fake")).is_fake
    smtp = build_email(
        Settings(
            EMAIL_PROVIDER="smtp",
            SMTP_HOST="smtp.example.com",
            SMTP_USERNAME="u",
            SMTP_PASSWORD="p",
            SENDING_DOMAIN="mantiqoutreach.example",
        )
    )
    assert not smtp.is_fake
    broken = build_email(Settings(EMAIL_PROVIDER="smtp", SMTP_HOST="", SMTP_USERNAME="", SMTP_PASSWORD=""))
    assert not broken.is_fake
    ok, detail = broken.healthcheck()
    assert not ok
    assert "missing" in detail


def test_registry_refuses_fakes_in_live_mode() -> None:
    from app.core.errors import ConfigError

    with pytest.raises(ConfigError):
        build_registry(
            Settings(
                OUTREACH_MODE="live",
                LLM_PROVIDER="fake",
                EMAIL_PROVIDER="fake",
                SENDER_NAME="A",
                SENDER_TITLE="B",
                SENDER_EMAIL="a@mantiqoutreach.example",
                PUBLIC_BASE_URL="https://outreach.example.com",
                COMPANY_ADDRESS="Cairo",
                COMPANY_PHONE="+201000000000",
                POC_OFFER_TEXT_AR="x",
                POC_OFFER_TEXT_EN="x",
                BOOKING_URL="https://cal.example.com/x",
                FEATURES_URL="https://outreach.example.com/features",
                DEMO_DURATION_MINUTES=20,
            )
        )


def test_status_report_flags_fakes() -> None:
    registry = build_registry(Settings(LLM_PROVIDER="fake", EMAIL_PROVIDER="fake", SEARCH_PROVIDER="fake"))
    report = registry.status_report()
    assert report["llm"]["is_fake"] is True
    assert report["llm"]["status"] == "sandbox"
    assert report["email"]["is_fake"] is True
    assert set(report) == {"llm", "email", "whatsapp", "search", "places", "verify", "calendar", "notify"}


@pytest.mark.asyncio
async def test_notify_fake_records_alerts() -> None:
    from app.adapters.notify.fake import FakeNotify

    notify = FakeNotify()
    ok, detail = await notify.send_alert(title="t", body="b", payload={})
    assert ok
    assert notify.alerts[-1]["title"] == "t"
    assert detail == "recorded"
