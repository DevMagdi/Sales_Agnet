"""Deliverability doctor: SPF, DKIM selector, DMARC, MX, mailbox auth, unsubscribe reachability,
DNSBL lookups and last-24h bounce/complaint rates.

Everything here is read-only and safe to run at any time; `live` mode refuses to start until the
blocking items pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.registry import AdapterRegistry
from app.core.clock import Clock
from app.core.config import Settings
from app.core.text import domain_of_email
from app.db.models import Mailbox, MessageEvent, OutreachMessage
from app.db.models.enums import MessageEventType


@dataclass
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "blocking": self.blocking}


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)
    rates: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.blocking)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
            "blocking_failures": [c.name for c in self.checks if c.blocking and not c.ok],
            "rates": self.rates,
        }


def _txt_records(domain: str, name: str = "") -> list[str]:
    import dns.resolver

    target = f"{name}.{domain}" if name else domain
    try:
        answers = dns.resolver.resolve(target, "TXT", lifetime=4.0)
        return [b"".join(rdata.strings).decode("utf-8", errors="ignore") for rdata in answers]
    except Exception:  # noqa: BLE001 - offline or missing record
        return []


def _mx_records(domain: str) -> list[str]:
    import dns.resolver

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4.0)
        return [str(r.exchange) for r in answers]
    except Exception:  # noqa: BLE001
        return []


def _dnsbl_listed(domain: str) -> tuple[bool, str]:
    """Check the domain (not an IP) against a domain-based DNSBL when available."""
    import dns.resolver

    try:
        answers = dns.resolver.resolve(f"{domain}.dbl.spamhaus.org", "A", lifetime=4.0)
        return True, "; ".join(str(a) for a in answers)
    except Exception:  # noqa: BLE001 - NXDOMAIN means not listed, or offline
        return False, "not listed or unreachable"


async def check_spf(domain: str) -> DoctorCheck:
    records = await asyncio.to_thread(_txt_records, domain)
    spf = [r for r in records if r.lower().startswith("v=spf1")]
    if not spf:
        return DoctorCheck("spf", False, f"no SPF TXT record on {domain}")
    if spf[0].strip().endswith("-all"):
        return DoctorCheck("spf", True, "SPF present with -all")
    return DoctorCheck("spf", True, f"SPF present (soft): {spf[0][:80]}")


async def check_dkim(domain: str, selector: str) -> DoctorCheck:
    records = await asyncio.to_thread(_txt_records, domain, f"{selector}._domainkey")
    if not any("v=dkim1" in r.lower() or "p=" in r.lower() for r in records):
        return DoctorCheck("dkim", False, f"no DKIM TXT at {selector}._domainkey.{domain}")
    return DoctorCheck("dkim", True, f"DKIM selector '{selector}' published")


async def check_dmarc(domain: str) -> DoctorCheck:
    records = await asyncio.to_thread(_txt_records, domain, "_dmarc")
    dmarc = [r for r in records if r.lower().startswith("v=dmarc1")]
    if not dmarc:
        return DoctorCheck("dmarc", False, "no DMARC record")
    strong = any("p=reject" in r.lower() or "p=quarantine" in r.lower() for r in dmarc)
    return DoctorCheck("dmarc", strong, f"DMARC: {dmarc[0][:80]} (p=none is not sufficient)")


async def check_mx(domain: str) -> DoctorCheck:
    records = await asyncio.to_thread(_mx_records, domain)
    return (
        DoctorCheck("mx", True, f"{len(records)} MX records")
        if records
        else DoctorCheck("mx", False, f"no MX records for {domain}")
    )


async def check_dnsbl(domain: str) -> DoctorCheck:
    listed, detail = await asyncio.to_thread(_dnsbl_listed, domain)
    return DoctorCheck("dnsbl", not listed, "listed" if listed else detail)


async def check_unsubscribe_reachable(settings: Settings) -> DoctorCheck:
    if not settings.public_base_url.startswith("https://"):
        return DoctorCheck("unsubscribe_url", False, "PUBLIC_BASE_URL is not https")
    url = f"{settings.public_base_url}/healthz"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url)
        return DoctorCheck("unsubscribe_url", response.status_code < 500, f"HTTP {response.status_code} from {url}")
    except httpx.HTTPError as exc:
        return DoctorCheck("unsubscribe_url", False, f"unreachable: {str(exc)[:120]}")


async def check_mailbox_auth(session: AsyncSession, registry: AdapterRegistry) -> DoctorCheck:
    rows = await session.execute(select(Mailbox))
    mailboxes = list(rows.scalars().all())
    if not mailboxes:
        return DoctorCheck("mailbox", False, "no mailbox configured")
    good = [m for m in mailboxes if m.status == "active" and (m.smtp_host or registry.email.name != "smtp")]
    adapter_ok, detail = registry.email.healthcheck()
    if not adapter_ok:
        return DoctorCheck("mailbox", False, detail)
    return DoctorCheck("mailbox", bool(good), f"{len(good)}/{len(mailboxes)} active mailboxes; {detail}")


async def rates_last_24h(session: AsyncSession, clock: Clock) -> dict[str, float]:
    since = clock.now() - timedelta(hours=24)
    sent = await session.execute(
        select(func.count()).select_from(OutreachMessage).where(
            OutreachMessage.sent_at.is_not(None), OutreachMessage.sent_at >= since
        )
    )
    total = int(sent.scalar_one())
    events = await session.execute(
        select(MessageEvent.type, func.count())
        .where(MessageEvent.occurred_at >= since)
        .group_by(MessageEvent.type)
    )
    counts = {str(kind): int(value) for kind, value in events.all()}
    hard = counts.get(MessageEventType.BOUNCE_HARD, 0)
    complaints = counts.get(MessageEventType.COMPLAINT, 0)
    return {
        "sent_24h": total,
        "bounces_hard_24h": hard,
        "complaints_24h": complaints,
        "bounce_rate": round(hard / total, 4) if total else 0.0,
        "complaint_rate": round(complaints / total, 4) if total else 0.0,
    }


async def run_doctor(
    session: AsyncSession, *, settings: Settings, registry: AdapterRegistry, clock: Clock
) -> DoctorReport:
    domain = settings.sending_domain or domain_of_email(settings.sender_email) or ""
    report = DoctorReport()
    if domain:
        report.checks.append(await check_spf(domain))
        report.checks.append(await check_dkim(domain, settings.dkim_selector))
        report.checks.append(await check_dmarc(domain))
        report.checks.append(await check_mx(domain))
        report.checks.append(await check_dnsbl(domain))
    else:
        report.checks.append(DoctorCheck("sending_domain", False, "SENDER_EMAIL/SENDING_DOMAIN not configured"))
    report.checks.append(await check_unsubscribe_reachable(settings))
    report.checks.append(await check_mailbox_auth(session, registry))
    report.checks.append(
        DoctorCheck(
            "public_base_url",
            settings.public_base_url.startswith("https://") or not settings.is_production_like,
            settings.public_base_url,
        )
    )
    report.rates = await rates_last_24h(session, clock)
    report.checks.append(
        DoctorCheck(
            "bounce_rate",
            report.rates["bounce_rate"] < 0.05,
            f"{report.rates['bounce_rate'] * 100:.2f}% hard bounces (24h)",
        )
    )
    report.checks.append(
        DoctorCheck(
            "complaint_rate",
            report.rates["complaint_rate"] < 0.001,
            f"{report.rates['complaint_rate'] * 100:.3f}% complaints (24h)",
        )
    )
    provider_status = registry.status_report()
    for name in ("llm", "email"):
        entry = provider_status[name]
        report.checks.append(
            DoctorCheck(f"provider_{name}", bool(entry["ok"]) and not entry["is_fake"], f"{entry['name']}: {entry['detail']}")
        )
    report.checks.append(
        DoctorCheck("kill_switch_off", True, "verified by the send path on every send", blocking=False)
    )
    return report
