"""LLM adapter factory."""

from __future__ import annotations

from app.adapters.llm.fake import DeterministicLLM
from app.adapters.llm.providers import AnthropicLLM, OpenAICompatibleLLM
from app.core.config import Settings
from app.core.errors import ConfigError

__all__ = ["AnthropicLLM", "DeterministicLLM", "OpenAICompatibleLLM", "build_llm"]


def build_llm(settings: Settings) -> AnthropicLLM | OpenAICompatibleLLM | DeterministicLLM:
    if settings.llm_provider == "fake":
        return DeterministicLLM()
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(
            api_key=settings.llm_api_key,
            model_fast=settings.llm_model_fast,
            model_strong=settings.llm_model_strong,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            price_input_per_m=settings.llm_price_input_per_m,
            price_output_per_m=settings.llm_price_output_per_m,
        )
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleLLM(
            api_key=settings.llm_api_key,
            model_fast=settings.llm_model_fast,
            model_strong=settings.llm_model_strong,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            price_input_per_m=settings.llm_price_input_per_m,
            price_output_per_m=settings.llm_price_output_per_m,
        )
    raise ConfigError(f"unknown LLM_PROVIDER: {settings.llm_provider}")
