"""Metered, validated LLM calls.

Every call: budget check → adapter call → JSON parse → Pydantic validation → one retry with the
validation error → fail safely. Tokens, latency and cost are persisted for every attempt.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import LLMAdapter
from app.adapters.llm.providers import parse_json_response
from app.core.clock import Clock
from app.core.config import Settings
from app.core.errors import AdapterError, BudgetExceeded, ValidationFailed
from app.core.logging import get_logger
from app.db.models import LLMCall
from app.services import budgets

logger = get_logger("llm")

T = TypeVar("T", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, version: str = "v1") -> tuple[str, str]:
    """Return (body, version_label) for a prompt file named `name@vN.md`."""
    path = PROMPTS_DIR / f"{name}@{version}.md"
    if not path.exists():
        raise ValidationFailed(f"prompt not found: {path.name}")
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        body = parts[2] if len(parts) >= 3 else text
    else:
        body = text
    return body.strip(), f"{name}@{version}"


def global_system_rules() -> str:
    rules, _ = load_prompt("system_rules")
    return rules


@dataclass
class LLMResponse:
    data: dict[str, Any]
    call: LLMCall
    attempts: int


async def call_json(
    session: AsyncSession,
    adapter: LLMAdapter,
    *,
    task: str,
    system: str,
    user: str,
    clock: Clock,
    settings: Settings,
    schema: type[T] | None = None,
    model_tier: Literal["fast", "strong"] = "fast",
    max_tokens: int = 1500,
    temperature: float = 0.2,
    prompt_version: str = "",
) -> LLMResponse:
    await budgets.check(session, clock, settings)
    system_full = f"{system.strip()}\n\n{global_system_rules()}"
    prompt_hash = hashlib.sha256(f"{system_full}\n{user}".encode()).hexdigest()
    attempt = 0
    validation_error = ""
    last_error = ""
    while attempt < 2:
        attempt += 1
        started = time.perf_counter()
        try:
            result = await adapter.complete_json(
                task=task,
                system=system_full,
                user=user if not validation_error else f"{user}\n\nPREVIOUS_ATTEMPT_INVALID: {validation_error}",
                model_tier=model_tier,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except AdapterError as exc:
            record = LLMCall(
                task=task,
                model=getattr(adapter, "name", "unknown"),
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                latency_ms=int((time.perf_counter() - started) * 1000),
                validated=False,
                error=str(exc)[:900],
            )
            session.add(record)
            await session.flush()
            raise
        latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
        record = LLMCall(
            task=task,
            model=result.model,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=latency_ms,
            validated=False,
        )
        if settings.log_llm_payloads:
            record.payload_request = {"system": system_full[:4000], "user": user[:8000]}
            record.payload_response = {"text": result.text[:8000]}
        try:
            data = parse_json_response(result.text)
            validated_data: dict[str, Any] = data
            if schema is not None:
                validated_data = schema.model_validate(data).model_dump()
        except (AdapterError, ValidationError) as exc:
            validation_error = str(exc)[:500]
            last_error = validation_error
            record.error = validation_error
            session.add(record)
            await session.flush()
            logger.warning("llm_validation_failed", task=task, attempt=attempt, error=validation_error[:200])
            continue
        record.validated = True
        session.add(record)
        await session.flush()
        return LLMResponse(data=validated_data, call=record, attempts=attempt)
    raise ValidationFailed(
        f"LLM output failed validation after {attempt} attempts: {last_error}",
        details={"task": task},
    )


async def assert_budget(session: AsyncSession, clock: Clock, settings: Settings) -> None:
    try:
        await budgets.check(session, clock, settings)
    except BudgetExceeded:
        raise
