"""Fake verification adapter with scripted results."""

from __future__ import annotations

from app.adapters.base import VerifyResult


class FakeVerify:
    name = "fake"
    is_fake = True

    def __init__(self, results: dict[str, str] | None = None, default: str = "valid") -> None:
        self.results = results or {}
        self.default = default
        self.calls: list[str] = []

    def healthcheck(self) -> tuple[bool, str]:
        return True, "fake verification adapter"

    async def verify(self, email: str) -> VerifyResult:
        self.calls.append(email)
        status = self.results.get(email.lower(), self.default)
        return VerifyResult(status=status, reason="scripted", provider=self.name)  # type: ignore[arg-type]
