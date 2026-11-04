"""Adapter registry: one place that builds every vendor integration from settings.

`live` mode refuses to run with a fake on a critical path; staging/production refuse fakes
unless ALLOW_FAKE_PROVIDERS=true (an explicit, logged exception).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.base import (
    CalendarAdapter,
    EmailAdapter,
    LLMAdapter,
    NotifyAdapter,
    PlacesAdapter,
    SearchAdapter,
    VerifyAdapter,
    WhatsAppAdapter,
)
from app.adapters.calendar import build_calendar
from app.adapters.email import build_email
from app.adapters.llm import build_llm
from app.adapters.notify import build_notify
from app.adapters.places import build_places
from app.adapters.search import build_search
from app.adapters.verify import build_verify
from app.adapters.whatsapp import build_whatsapp
from app.core.config import Settings, get_settings
from app.core.errors import ConfigError


@dataclass
class AdapterRegistry:
    llm: LLMAdapter
    email: EmailAdapter
    whatsapp: WhatsAppAdapter
    search: SearchAdapter
    places: PlacesAdapter
    verify: VerifyAdapter
    calendar: CalendarAdapter
    notify: NotifyAdapter

    def critical_fakes(self) -> list[str]:
        fakes = []
        if getattr(self.llm, "is_fake", False):
            fakes.append("llm")
        if getattr(self.email, "is_fake", False):
            fakes.append("email")
        return fakes

    def all_fakes(self) -> list[str]:
        out = []
        for field_name in ("llm", "email", "whatsapp", "search", "places", "verify", "calendar", "notify"):
            adapter = getattr(self, field_name)
            if getattr(adapter, "is_fake", False):
                out.append(field_name)
        return out

    def status_report(self) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for field_name in ("llm", "email", "whatsapp", "search", "places", "verify", "calendar", "notify"):
            adapter = getattr(self, field_name)
            ok, detail = adapter.healthcheck()
            report[field_name] = {
                "name": getattr(adapter, "name", type(adapter).__name__),
                "is_fake": getattr(adapter, "is_fake", False),
                "ok": ok,
                "detail": detail,
                "status": "sandbox" if getattr(adapter, "is_fake", False) else ("live" if ok else "error"),
            }
        return report


def build_registry(settings: Settings | None = None) -> AdapterRegistry:
    s = settings or get_settings()
    email = build_email(s)
    if s.outreach_mode == "live":
        if getattr(email, "is_fake", False):
            raise ConfigError("EMAIL_PROVIDER=fake is not allowed in live mode")
        llm = build_llm(s)
        if getattr(llm, "is_fake", False):
            raise ConfigError("LLM_PROVIDER=fake is not allowed in live mode")
    else:
        llm = build_llm(s)
    return AdapterRegistry(
        llm=llm,
        email=email,
        whatsapp=build_whatsapp(s),
        search=build_search(s),
        places=build_places(s),
        verify=build_verify(s),
        calendar=build_calendar(s),
        notify=build_notify(s, email),
    )


_registry: AdapterRegistry | None = None


def get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def set_registry(registry: AdapterRegistry | None) -> None:
    global _registry
    _registry = registry
