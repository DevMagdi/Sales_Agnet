"""Email adapter factory."""

from __future__ import annotations

from app.adapters.email.api import ApiEmailAdapter
from app.adapters.email.fake import FakeEmailAdapter
from app.adapters.email.smtp import SMTPEmailAdapter
from app.core.config import Settings
from app.core.errors import ConfigError

__all__ = ["ApiEmailAdapter", "FakeEmailAdapter", "SMTPEmailAdapter", "build_email"]


def build_email(settings: Settings) -> SMTPEmailAdapter | ApiEmailAdapter | FakeEmailAdapter:
    if settings.email_provider == "fake":
        return FakeEmailAdapter()
    if settings.email_provider == "smtp":
        return SMTPEmailAdapter(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            webhook_secret=settings.email_webhook_secret,
            dkim_private_key=settings.dkim_private_key,
            dkim_selector=settings.dkim_selector,
            sending_domain=settings.sending_domain,
        )
    if settings.email_provider == "api":
        return ApiEmailAdapter(
            api_url=settings.email_api_url,
            api_key=settings.email_api_key,
            webhook_secret=settings.email_webhook_secret,
            from_email=settings.sender_email,
        )
    raise ConfigError(f"unknown EMAIL_PROVIDER: {settings.email_provider}")
