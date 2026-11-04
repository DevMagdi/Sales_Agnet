"""Typed configuration. Every knob is an environment variable; nothing is hard-coded."""

from __future__ import annotations

import functools
import re
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OutreachMode = Literal["dry_run", "sandbox", "live"]
AppEnv = Literal["dev", "test", "staging", "production"]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- identity -------------------------------------------------------
    app_env: AppEnv = Field("dev", alias="APP_ENV")
    app_name: str = "Mantiq Outreach Agent"
    secret_key: str = Field("dev-insecure-secret-change-me", alias="SECRET_KEY")
    encryption_master_key: str = Field("", alias="ENCRYPTION_MASTER_KEY")
    public_base_url: str = Field("http://localhost:8000", alias="PUBLIC_BASE_URL")
    cors_origins: str = Field("http://localhost:5173", alias="CORS_ORIGINS")

    # --- storage --------------------------------------------------------
    database_url: str = Field("sqlite+aiosqlite:///./mantiq.db", alias="DATABASE_URL")
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")

    # --- operating mode -------------------------------------------------
    outreach_mode: OutreachMode = Field("dry_run", alias="OUTREACH_MODE")
    sandbox_allowlist: str = Field("", alias="SANDBOX_ALLOWLIST")
    tz_default: str = Field("Africa/Cairo", alias="TZ_DEFAULT")
    allow_fake_providers: bool = Field(False, alias="ALLOW_FAKE_PROVIDERS")

    # --- LLM ------------------------------------------------------------
    llm_provider: Literal["fake", "anthropic", "openai_compatible"] = Field("fake", alias="LLM_PROVIDER")
    llm_api_key: str = Field("", alias="LLM_API_KEY")
    llm_base_url: str = Field("", alias="LLM_BASE_URL")
    llm_model_fast: str = Field("", alias="LLM_MODEL_FAST")
    llm_model_strong: str = Field("", alias="LLM_MODEL_STRONG")
    llm_daily_budget_usd: float = Field(5.0, alias="LLM_DAILY_BUDGET_USD")
    llm_timeout_seconds: float = Field(60.0, alias="LLM_TIMEOUT_SECONDS")
    llm_price_input_per_m: float = Field(1.0, alias="LLM_PRICE_INPUT_PER_M")
    llm_price_output_per_m: float = Field(3.0, alias="LLM_PRICE_OUTPUT_PER_M")

    # --- external data providers ----------------------------------------
    search_provider: Literal["fake", "serpapi", "brave"] = Field("fake", alias="SEARCH_PROVIDER")
    search_api_key: str = Field("", alias="SEARCH_API_KEY")
    places_api_key: str = Field("", alias="PLACES_API_KEY")
    api_daily_budget_usd: float = Field(5.0, alias="API_DAILY_BUDGET_USD")
    verify_provider: Literal["fake", "local", "zerobounce"] = Field("local", alias="VERIFY_PROVIDER")
    verify_api_key: str = Field("", alias="VERIFY_API_KEY")

    # --- email ----------------------------------------------------------
    email_provider: Literal["fake", "smtp", "api"] = Field("fake", alias="EMAIL_PROVIDER")
    smtp_host: str = Field("", alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_username: str = Field("", alias="SMTP_USERNAME")
    smtp_password: str = Field("", alias="SMTP_PASSWORD")
    smtp_use_tls: bool = Field(True, alias="SMTP_USE_TLS")
    imap_host: str = Field("", alias="IMAP_HOST")
    imap_port: int = Field(993, alias="IMAP_PORT")
    imap_username: str = Field("", alias="IMAP_USERNAME")
    imap_password: str = Field("", alias="IMAP_PASSWORD")
    email_api_url: str = Field("", alias="EMAIL_API_URL")
    email_api_key: str = Field("", alias="EMAIL_API_KEY")
    email_webhook_secret: str = Field("", alias="EMAIL_WEBHOOK_SECRET")
    dkim_selector: str = Field("default", alias="DKIM_SELECTOR")
    dkim_private_key: str = Field("", alias="DKIM_PRIVATE_KEY")
    sending_domain: str = Field("", alias="SENDING_DOMAIN")

    # --- whatsapp -------------------------------------------------------
    whatsapp_phone_number_id: str = Field("", alias="WHATSAPP_PHONE_NUMBER_ID")
    whatsapp_token: str = Field("", alias="WHATSAPP_TOKEN")
    whatsapp_app_secret: str = Field("", alias="WHATSAPP_APP_SECRET")
    whatsapp_verify_token: str = Field("", alias="WHATSAPP_VERIFY_TOKEN")
    whatsapp_api_url: str = Field("https://graph.facebook.com/v20.0", alias="WHATSAPP_API_URL")

    # --- notifications / booking ---------------------------------------
    notify_webhook_url: str = Field("", alias="NOTIFY_WEBHOOK_URL")
    notify_email: str = Field("", alias="NOTIFY_EMAIL")
    booking_url: str = Field("", alias="BOOKING_URL")
    features_url: str = Field("", alias="FEATURES_URL")
    calendar_provider: Literal["fake", "link"] = Field("link", alias="CALENDAR_PROVIDER")

    # --- sender identity ------------------------------------------------
    sender_name: str = Field("", alias="SENDER_NAME")
    sender_title: str = Field("", alias="SENDER_TITLE")
    sender_email: str = Field("", alias="SENDER_EMAIL")
    company_name: str = Field("Mantiq Technologies", alias="COMPANY_NAME")
    company_address: str = Field("", alias="COMPANY_ADDRESS")
    company_phone: str = Field("", alias="COMPANY_PHONE")
    poc_offer_text_ar: str = Field("", alias="POC_OFFER_TEXT_AR")
    poc_offer_text_en: str = Field("", alias="POC_OFFER_TEXT_EN")
    demo_duration_minutes: int = Field(0, alias="DEMO_DURATION_MINUTES")

    # --- crawler --------------------------------------------------------
    crawler_user_agent: str = Field("MantiqOutreachBot/1.0", alias="CRAWLER_USER_AGENT")
    crawler_bot_url: str = Field("", alias="CRAWLER_BOT_URL")
    crawl_max_pages: int = Field(6, alias="CRAWL_MAX_PAGES")
    crawl_delay_seconds: float = Field(1.0, alias="CRAWL_DELAY_SECONDS")
    crawl_timeout_seconds: float = Field(10.0, alias="CRAWL_TIMEOUT_SECONDS")
    crawl_max_page_bytes: int = Field(2_000_000, alias="CRAWL_MAX_PAGE_BYTES")
    crawl_recrawl_after_days: int = Field(90, alias="CRAWL_RECRAWL_AFTER_DAYS")
    crawl_allow_local_hosts: bool = Field(False, alias="CRAWL_ALLOW_LOCAL_HOSTS")

    # --- flags ----------------------------------------------------------
    click_tracking_enabled: bool = Field(False, alias="CLICK_TRACKING_ENABLED")
    open_tracking_enabled: bool = Field(False, alias="OPEN_TRACKING_ENABLED")
    log_llm_payloads: bool = Field(False, alias="LOG_LLM_PAYLOADS")
    celery_task_always_eager: bool = Field(False, alias="CELERY_TASK_ALWAYS_EAGER")

    # --- retention ------------------------------------------------------
    retention_page_text_days: int = Field(180, alias="RETENTION_DAYS_PAGE_TEXT")
    retention_closed_contact_days: int = Field(365, alias="RETENTION_DAYS_CLOSED_CONTACTS")
    retention_message_days: int = Field(2555, alias="RETENTION_DAYS_MESSAGES")

    # --- rate limits / safety -------------------------------------------
    login_max_attempts: int = Field(8, alias="LOGIN_MAX_ATTEMPTS")
    login_lockout_seconds: int = Field(900, alias="LOGIN_LOCKOUT_SECONDS")
    access_token_minutes: int = Field(30, alias="ACCESS_TOKEN_MINUTES")
    refresh_token_days: int = Field(14, alias="REFRESH_TOKEN_DAYS")
    unsubscribe_token_ttl_days: int = Field(365, alias="UNSUBSCRIBE_TOKEN_TTL_DAYS")

    @field_validator("public_base_url", "features_url", "booking_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("llm_daily_budget_usd", "api_daily_budget_usd")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("budget must be >= 0")
        return v

    @property
    def is_production_like(self) -> bool:
        return self.app_env in ("staging", "production")

    @property
    def sandbox_recipients(self) -> list[str]:
        return [p.strip().lower() for p in self.sandbox_allowlist.split(",") if p.strip()]

    @property
    def cors_list(self) -> list[str]:
        return [p.strip() for p in self.cors_origins.split(",") if p.strip()]

    @property
    def bot_url(self) -> str:
        return self.crawler_bot_url or f"{self.public_base_url}/bot"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def live_mode_blockers(s: Settings) -> list[str]:
    """Return the reasons `live` mode may not start. Empty list == allowed.

    This is the automatable half of the go-live checklist; the human half lives in
    docs/GO_LIVE_CHECKLIST.md.
    """
    problems: list[str] = []
    for name, value in (
        ("SENDER_NAME", s.sender_name),
        ("SENDER_TITLE", s.sender_title),
        ("SENDER_EMAIL", s.sender_email),
        ("COMPANY_ADDRESS", s.company_address),
        ("COMPANY_PHONE", s.company_phone),
        ("POC_OFFER_TEXT_AR", s.poc_offer_text_ar),
        ("POC_OFFER_TEXT_EN", s.poc_offer_text_en),
        ("BOOKING_URL", s.booking_url),
        ("FEATURES_URL", s.features_url),
    ):
        if not value:
            problems.append(f"{name} is required in live mode")
    if s.demo_duration_minutes <= 0:
        problems.append("DEMO_DURATION_MINUTES must be > 0 in live mode")
    if not s.sender_email or not _EMAIL_RE.match(s.sender_email):
        problems.append("SENDER_EMAIL must be a valid email address")
    if not s.public_base_url.startswith("https://") and s.is_production_like:
        problems.append("PUBLIC_BASE_URL must be https in staging/production")
    if s.email_provider == "fake":
        problems.append("EMAIL_PROVIDER must be a real adapter in live mode")
    elif s.email_provider == "smtp" and not (s.smtp_host and s.smtp_username and s.smtp_password):
        problems.append("SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD are required for EMAIL_PROVIDER=smtp")
    elif s.email_provider == "api" and not (s.email_api_url and s.email_api_key):
        problems.append("EMAIL_API_URL/EMAIL_API_KEY are required for EMAIL_PROVIDER=api")
    if s.llm_provider == "fake":
        problems.append("LLM_PROVIDER must be a real adapter in live mode")
    elif not s.llm_api_key:
        problems.append("LLM_API_KEY is required for a real LLM provider")
    if s.llm_provider != "fake" and not (s.llm_model_fast and s.llm_model_strong):
        problems.append("LLM_MODEL_FAST and LLM_MODEL_STRONG are required")
    if s.encryption_master_key in ("", "dev-insecure-encryption-key"):
        problems.append("ENCRYPTION_MASTER_KEY must be set to a strong key")
    if s.secret_key in ("", "dev-insecure-secret-change-me"):
        problems.append("SECRET_KEY must be set to a strong key")
    if not s.public_base_url.startswith("https://") and not s.allow_fake_providers:
        problems.append("PUBLIC_BASE_URL must be https so unsubscribe links work publicly")
    return problems


def startup_problems(s: Settings) -> list[str]:
    """Problems that stop the process from booting at all."""
    problems: list[str] = []
    if s.is_production_like and not s.allow_fake_providers:
        if s.llm_provider == "fake" or s.email_provider == "fake":
            problems.append(
                "fake provider on a critical path (LLM/email) in staging/production; "
                "set real adapters or ALLOW_FAKE_PROVIDERS=true for a deliberate, logged exception"
            )
        if s.search_provider == "fake":
            problems.append("SEARCH_PROVIDER=fake in staging/production without ALLOW_FAKE_PROVIDERS=true")
    if s.outreach_mode == "live":
        problems.extend(live_mode_blockers(s))
    return problems
