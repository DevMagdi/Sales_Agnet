"""Local email verification: syntax, blocklists, MX lookup. No address is ever guessed."""

from __future__ import annotations

import asyncio

from app.adapters.base import VerifyResult
from app.core.text import domain_of_email
from app.core.yamlconf import load_blocklist


def _mx_exists(domain: str) -> bool | None:
    """Return True/False, or None when DNS cannot be consulted (offline)."""
    try:
        import dns.resolver

        answers = dns.resolver.resolve(domain, "MX", lifetime=3.0)
        return len(answers) > 0
    except Exception:  # noqa: BLE001 - offline / NXDOMAIN / timeout all mean "unknown"
        return None


class LocalVerifyAdapter:
    name = "local"
    is_fake = False

    def healthcheck(self) -> tuple[bool, str]:
        return True, "local syntax + blocklist + MX checks"

    async def verify(self, email: str) -> VerifyResult:
        blocklist = load_blocklist()
        local, _, domain = email.strip().lower().partition("@")
        if not local or not domain or "." not in domain or " " in email:
            return VerifyResult(status="invalid", reason="syntax", provider=self.name)
        if domain in blocklist.disposable:
            return VerifyResult(status="disposable", reason="disposable_domain", provider=self.name)
        if domain in blocklist.free_mail:
            return VerifyResult(status="risky", reason="free_mail_domain", provider=self.name)
        mx = await asyncio.to_thread(_mx_exists, domain)
        if mx is False:
            return VerifyResult(status="invalid", reason="no_mx_record", provider=self.name)
        if mx is None:
            return VerifyResult(status="risky", reason="mx_unverified_offline", provider=self.name)
        # role addresses are valid but slightly less certain
        return VerifyResult(status="valid", reason="mx_ok", provider=self.name)

    async def verify_domain(self, email: str) -> VerifyResult:
        return await self.verify(email)


def domain_has_mx(email: str) -> bool | None:
    return _mx_exists(domain_of_email(email))
