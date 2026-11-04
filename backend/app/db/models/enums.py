"""String enumerations shared by models, services and the API."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    SALES = "sales"
    VIEWER = "viewer"


class CompanyStatus(StrEnum):
    DISCOVERED = "discovered"
    ENRICHED = "enriched"
    RESEARCHED = "researched"
    QUALIFIED = "qualified"
    QUEUED = "queued"
    EXCLUDED = "excluded"


class ProspectState(StrEnum):
    DISCOVERED = "discovered"
    ENRICHED = "enriched"
    RESEARCHED = "researched"
    QUALIFIED = "qualified"
    QUEUED = "queued"
    COMPOSING = "composing"
    STEP1_SENT = "step1_sent"
    FOLLOWUP_1_SENT = "followup_1_sent"
    FOLLOWUP_2_SENT = "followup_2_sent"
    NO_RESPONSE = "no_response"
    INTERESTED = "interested"
    WANTS_INFO = "wants_info"
    MEETING_PROPOSAL = "meeting_proposal"
    HANDED_OFF = "handed_off"
    DEMO_BOOKED = "demo_booked"
    DEMO_HELD = "demo_held"
    WON = "won"
    LOST = "lost"
    REFERRAL_FOLLOWED = "referral_followed"
    NOT_NOW = "not_now"
    NOT_INTERESTED = "not_interested"
    SNOOZED = "snoozed"
    CLOSED_LOST = "closed_lost"
    DO_NOT_CONTACT = "do_not_contact"
    BOUNCED = "bounced"
    PAUSED = "paused"
    BLOCKED_BY_GATE = "blocked_by_gate"
    EXCLUDED = "excluded"


class EmailStatus(StrEnum):
    UNVERIFIED = "unverified"
    VALID = "valid"
    RISKY = "risky"
    INVALID = "invalid"
    DISPOSABLE = "disposable"


class RoleCategory(StrEnum):
    PROCUREMENT = "procurement"
    PURCHASING = "purchasing"
    SUPPLY_CHAIN = "supply_chain"
    OPERATIONS = "operations"
    OWNER_GM = "owner_gm"
    FINANCE = "finance"
    GENERAL = "general"
    UNKNOWN = "unknown"


class Channel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class Direction(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class TemplateStep(StrEnum):
    INITIAL = "initial"
    FOLLOWUP_1 = "followup_1"
    FOLLOWUP_2 = "followup_2"
    BREAKUP = "breakup"
    REPLY_INFO = "reply_info"
    REPLY_BOOKING = "reply_booking"
    REPLY_UNSUBSCRIBE = "reply_unsubscribe"
    REPLY_REFERRAL_THANKS = "reply_referral_thanks"


class MessageStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    WOULD_SEND = "would_send"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED_HARD = "bounced_hard"
    BOUNCED_SOFT = "bounced_soft"
    FAILED = "failed"
    BLOCKED = "blocked"
    REPLIED = "replied"


class MessageEventType(StrEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCE_HARD = "bounce_hard"
    BOUNCE_SOFT = "bounce_soft"
    COMPLAINT = "complaint"
    CLICK = "click"
    UNSUBSCRIBE = "unsubscribe"
    OPEN = "open"
    REPLY = "reply"


class ReplyClass(StrEnum):
    INTERESTED = "interested"
    WANTS_INFO = "wants_info"
    MEETING_PROPOSAL = "meeting_proposal"
    REFERRAL = "referral"
    NOT_NOW = "not_now"
    NOT_INTERESTED = "not_interested"
    UNSUBSCRIBE = "unsubscribe"
    OUT_OF_OFFICE = "out_of_office"
    AUTO_REPLY = "auto_reply"
    BOUNCE = "bounce"
    COMPLAINT = "complaint"
    QUESTION_TECHNICAL = "question_technical"
    QUESTION_PRICING = "question_pricing"
    UNCLEAR = "unclear"


class ReplyStatus(StrEnum):
    NEW = "new"
    AUTO_HANDLED = "auto_handled"
    NEEDS_HUMAN = "needs_human"
    HANDLED = "handled"


class TaskType(StrEnum):
    REVIEW_MESSAGE = "review_message"
    REPLY_DRAFT = "reply_draft"
    CALL_PROSPECT = "call_prospect"
    LINKEDIN_MANUAL = "linkedin_manual"
    BOOK_DEMO = "book_demo"
    RESOLVE_ERROR = "resolve_error"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class DemoStatus(StrEnum):
    REQUESTED = "requested"
    LINK_SENT = "link_sent"
    BOOKED = "booked"
    HELD = "held"
    NO_SHOW = "no_show"
    WON = "won"
    LOST = "lost"


class SuppressionKind(StrEnum):
    EMAIL = "email"
    DOMAIN = "domain"
    PHONE = "phone"


class SuppressionReason(StrEnum):
    UNSUBSCRIBE = "unsubscribe"
    REPLY_STOP = "reply_stop"
    BOUNCE_HARD = "bounce_hard"
    COMPLAINT = "complaint"
    MANUAL = "manual"
    ERASURE = "erasure"
    COMPETITOR = "competitor"
    LEGAL = "legal"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED_BY_CIRCUIT_BREAKER = "stopped_by_circuit_breaker"


class MailboxStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class ConsentStatus(StrEnum):
    OPTED_IN = "opted_in"
    OPTED_OUT = "opted_out"


class LLMTask(StrEnum):
    RESEARCH = "research"
    PLAYBOOK_SELECT = "playbook_select"
    COMPOSE_SLOT = "compose_slot"
    CLAIM_VERIFY = "claim_verify"
    CLASSIFY_REPLY = "classify_reply"
    DRAFT_REPLY = "draft_reply"


class SourceKind(StrEnum):
    CSV = "csv"
    PLACES = "places"
    SEARCH = "search"
    DIRECTORY = "directory"
    MANUAL = "manual"


class CompanyLanguage(StrEnum):
    AR = "ar"
    EN = "en"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SizeHint(StrEnum):
    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    UNKNOWN = "unknown"
