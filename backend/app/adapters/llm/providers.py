"""Real LLM adapters: Anthropic Messages API and any OpenAI-compatible endpoint.

Rules implemented here:
  * forced JSON output (structured), low temperature
  * retries only on 429/5xx/transport errors; fail fast on other 4xx
  * per-call token + cost metering using configured prices
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from app.adapters.base import LLMResult
from app.core.errors import AdapterError

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class _RetryableHTTP(Exception):
    pass


def _should_retry(exc: BaseException) -> bool:
    return isinstance(exc, (_RetryableHTTP, httpx.TransportError, httpx.TimeoutException))


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


def parse_json_response(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a model response, tolerating prose around it."""
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise AdapterError("LLM did not return JSON") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AdapterError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AdapterError("LLM JSON response must be an object")
    return data


class _HTTPLLM:
    name = "llm"
    is_fake = False
    default_base_url = ""

    def __init__(
        self,
        api_key: str,
        model_fast: str,
        model_strong: str,
        base_url: str = "",
        *,
        timeout: float = 60.0,
        price_input_per_m: float = 1.0,
        price_output_per_m: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model_fast = model_fast
        self._model_strong = model_strong
        self._base_url = (base_url or self.default_base_url).rstrip("/")
        self._timeout = timeout
        self._price_in = price_input_per_m
        self._price_out = price_output_per_m
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _model(self, tier: Literal["fast", "strong"]) -> str:
        model = self._model_strong if tier == "strong" else self._model_fast
        if not model:
            raise AdapterError("LLM model name is not configured (LLM_MODEL_FAST/LLM_MODEL_STRONG)")
        return model

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return round(tokens_in / 1_000_000 * self._price_in + tokens_out / 1_000_000 * self._price_out, 6)

    async def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = await self._http().post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise _RetryableHTTP(str(exc)) from exc
        if response.status_code in _RETRYABLE_STATUS:
            raise _RetryableHTTP(f"HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            raise AdapterError(f"LLM request failed ({response.status_code}): {response.text[:300]}")
        try:
            data: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise AdapterError("LLM returned non-JSON body") from exc
        data["_latency_ms"] = int((time.perf_counter() - started) * 1000)
        return data

    def healthcheck(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "LLM_API_KEY missing"
        if not (self._model_fast or self._model_strong):
            return False, "LLM model names missing"
        return True, f"configured ({self.name})"


class AnthropicLLM(_HTTPLLM):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com"

    @retry(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=8),
        reraise=True,
    )
    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        model_tier: Literal["fast", "strong"] = "fast",
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> LLMResult:
        model = self._model(model_tier)
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = await self._post(
            f"{self._base_url}/v1/messages",
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            body,
        )
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        usage = data.get("usage") or {}
        t_in = int(usage.get("input_tokens", 0))
        t_out = int(usage.get("output_tokens", 0))
        return LLMResult(
            text=text,
            model=model,
            input_tokens=t_in,
            output_tokens=t_out,
            cost_usd=self._cost(t_in, t_out),
            latency_ms=int(data.get("_latency_ms", 0)),
            raw={"task": task, "stop_reason": data.get("stop_reason")},
        )


class OpenAICompatibleLLM(_HTTPLLM):
    name = "openai_compatible"
    default_base_url = "https://api.openai.com/v1"

    @retry(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=1, max=8),
        reraise=True,
    )
    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        model_tier: Literal["fast", "strong"] = "fast",
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> LLMResult:
        model = self._model(model_tier)
        body = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = await self._post(
            f"{self._base_url}/chat/completions",
            {"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"},
            body,
        )
        choices = data.get("choices") or [{}]
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        text = message.get("content", "") if isinstance(message, dict) else ""
        usage = data.get("usage") or {}
        t_in = int(usage.get("prompt_tokens", 0))
        t_out = int(usage.get("completion_tokens", 0))
        return LLMResult(
            text=text,
            model=model,
            input_tokens=t_in,
            output_tokens=t_out,
            cost_usd=self._cost(t_in, t_out),
            latency_ms=int(data.get("_latency_ms", 0)),
            raw={"task": task, "finish_reason": (choices[0] or {}).get("finish_reason")},
        )
