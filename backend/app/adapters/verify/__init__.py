"""Verification adapter factory."""

from __future__ import annotations

from app.adapters.verify.external import ExternalVerifyAdapter
from app.adapters.verify.fake import FakeVerify
from app.adapters.verify.local import LocalVerifyAdapter
from app.core.config import Settings

__all__ = ["ExternalVerifyAdapter", "FakeVerify", "LocalVerifyAdapter", "build_verify"]


def build_verify(settings: Settings) -> LocalVerifyAdapter | ExternalVerifyAdapter | FakeVerify:
    if settings.verify_provider == "fake":
        return FakeVerify()
    if settings.verify_provider == "zerobounce" and settings.verify_api_key:
        return ExternalVerifyAdapter(settings.verify_api_key)
    return LocalVerifyAdapter()
