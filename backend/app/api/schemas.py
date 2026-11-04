"""API request/response models (Pydantic v2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str = ""


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]


class RefreshRequest(BaseModel):
    refresh_token: str


class MeResponse(BaseModel):
    id: str
    email: str
    name: str
    role: str
    locale: str
    mfa_enabled: bool


class CompanyUpdate(BaseModel):
    name: str | None = None
    name_ar: str | None = None
    city: str | None = None
    industrial_zone: str | None = None
    industry: str | None = None
    website: str | None = None
    notes: str | None = None
    exclude: bool | None = None
    excluded_reason: str | None = None


class CompanyCreate(BaseModel):
    name: str
    domain: str | None = None
    website: str | None = None
    country: str = "EG"
    city: str | None = None
    industry: str | None = None
    email: str | None = None
    phone: str | None = None


class ContactCreate(BaseModel):
    email: str | None = None
    phone: str | None = None
    full_name: str | None = None
    role_title: str | None = None
    source_url: str | None = None


class CsvPreviewRequest(BaseModel):
    content: str
    mapping: dict[str, str | None] | None = None


class CsvCommitRequest(BaseModel):
    content: str = Field(max_length=20_000_000)
    mapping: dict[str, str | None] | None = None
    filename: str = "import.csv"
    save_mapping_as: str | None = None


class DiscoveryJobCreate(BaseModel):
    kind: Literal["search", "places", "directory"] = "search"
    query: str = ""
    country: str = "EG"
    city: str | None = None
    industry: str | None = None
    max_results: int = 50
    budget_cap_usd: float = 2.0


class CampaignCreate(BaseModel):
    name: str
    industry_filter: list[str] = Field(default_factory=list)
    country_filter: list[str] = Field(default_factory=lambda: ["EG"])
    city_filter: list[str] = Field(default_factory=list)
    min_icp_score: int = 60
    language_policy: Literal["auto", "ar", "en"] = "auto"
    template_set: str = "default"
    mailbox_pool_id: str | None = None
    daily_cap: int = 20
    send_windows: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Africa/Cairo"
    review_first_n: int = 0
    auto_reply_classes: list[str] = Field(default_factory=lambda: ["unsubscribe", "wants_info", "meeting_proposal"])
    channels_enabled: list[str] = Field(default_factory=lambda: ["email"])
    max_followups: int = 2
    followup_gaps_days: list[int] = Field(default_factory=lambda: [3, 7])
    ramp_schedule: dict[str, Any] = Field(default_factory=dict)
    allow_risky_emails: bool = False


class CampaignUpdate(CampaignCreate):
    name: str | None = None  # type: ignore[assignment]


class CampaignStatusUpdate(BaseModel):
    action: Literal["activate", "pause", "stop", "resume"]
    reason: str = ""


class PreviewRequest(BaseModel):
    company_id: str
    contact_id: str | None = None
    step: Literal["initial", "followup_1", "followup_2", "breakup"] = "initial"


class MessageReview(BaseModel):
    action: Literal["approve", "reject"]
    subject: str | None = None
    body_text: str | None = None
    reason: str = ""


class ReplyOverride(BaseModel):
    classification: Literal[
        "interested",
        "wants_info",
        "meeting_proposal",
        "referral",
        "not_now",
        "not_interested",
        "unsubscribe",
        "out_of_office",
        "auto_reply",
        "bounce",
        "complaint",
        "question_technical",
        "question_pricing",
        "unclear",
    ]
    reason: str = ""


class SendReplyRequest(BaseModel):
    subject: str
    body_text: str


class TaskUpdate(BaseModel):
    status: Literal["open", "in_progress", "done", "cancelled"]
    assigned_to: str | None = None
    note: str = ""


class DemoUpdate(BaseModel):
    status: Literal["requested", "link_sent", "booked", "held", "no_show", "won", "lost"]
    booked_at: str | None = None
    notes: str = ""


class SuppressionCreate(BaseModel):
    kind: Literal["email", "domain", "phone"]
    value: str
    reason: Literal[
        "unsubscribe", "reply_stop", "bounce_hard", "complaint", "manual", "erasure", "competitor", "legal"
    ] = "manual"
    note: str = ""


class SuppressionImportRequest(BaseModel):
    content: str = Field(max_length=5_000_000)
    reason: Literal[
        "unsubscribe", "reply_stop", "bounce_hard", "complaint", "manual", "erasure", "competitor", "legal"
    ] = "manual"


class TemplateCreate(BaseModel):
    code: str
    locale: Literal["ar", "en"]
    step: str
    subject: str = ""
    body_text: str
    channel: Literal["email", "whatsapp"] = "email"


class MailboxCreate(BaseModel):
    from_email: EmailStr
    from_name: str
    pool_id: str | None = None
    daily_cap: int = 20
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_password: str | None = None
    signature_text: str = ""
    physical_address: str = ""
    phone: str = ""


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    name: str = ""
    role: Literal["admin", "sales", "viewer"] = "sales"
    locale: Literal["ar", "en"] = "en"


class MailboxStatusUpdate(BaseModel):
    status: Literal["active", "paused", "warmup", "retired"]


class KillSwitchRequest(BaseModel):
    active: bool
    reason: str = ""


class ModeRequest(BaseModel):
    mode: Literal["dry_run", "sandbox", "live"]
    confirm_live: bool = False


class BudgetRequest(BaseModel):
    llm_daily_usd: float
    api_daily_usd: float


class WebhookAck(BaseModel):
    ok: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)
