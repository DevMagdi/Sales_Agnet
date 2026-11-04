"""WhatsApp adapter factory."""

from __future__ import annotations

from app.adapters.whatsapp.cloud import WhatsAppCloudAdapter
from app.adapters.whatsapp.fake import FakeWhatsAppAdapter
from app.core.config import Settings
from app.core.errors import ConfigError

__all__ = ["FakeWhatsAppAdapter", "WhatsAppCloudAdapter", "build_whatsapp"]


def build_whatsapp(settings: Settings) -> WhatsAppCloudAdapter | FakeWhatsAppAdapter:
    if settings.whatsapp_token and settings.whatsapp_app_secret and settings.whatsapp_phone_number_id:
        return WhatsAppCloudAdapter(
            phone_number_id=settings.whatsapp_phone_number_id,
            token=settings.whatsapp_token,
            app_secret=settings.whatsapp_app_secret,
            api_url=settings.whatsapp_api_url,
        )
    if settings.is_production_like and not settings.allow_fake_providers:
        raise ConfigError(
            "WhatsApp credentials missing in staging/production; set WHATSAPP_* or ALLOW_FAKE_PROVIDERS=true"
        )
    return FakeWhatsAppAdapter()
