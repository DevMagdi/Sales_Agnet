"""Application error hierarchy, mapped to a single JSON error envelope by the API."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class Blocked(AppError):
    """Deterministic safety rule refused an action. Never bypassable."""

    status_code = 409
    code = "blocked"


class AdapterError(AppError):
    status_code = 502
    code = "adapter_error"


class ConfigError(AppError):
    status_code = 500
    code = "config_error"


class BudgetExceeded(AppError):
    status_code = 429
    code = "budget_exceeded"
