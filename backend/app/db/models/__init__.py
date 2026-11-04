"""All ORM models. Importing this module registers every table on Base.metadata."""

from __future__ import annotations

from app.db.models.campaign import (
    Campaign,
    CampaignProspect,
    DemoRequest,
    InboundMessage,
    Mailbox,
    MailboxPool,
    MessageEvent,
    OutreachMessage,
    Reply,
    Task,
    Template,
)
from app.db.models.company import (
    Company,
    Contact,
    DiscoveryJob,
    ImportBatch,
    IndustryPlaybookRow,
    PageCache,
    ResearchSnapshot,
    SavedMapping,
)
from app.db.models.core import AuditLog, Counter, RefreshToken, Setting, User
from app.db.models.safety import (
    ApiCost,
    ConsentRecord,
    LLMCall,
    OutboxEvent,
    SuppressionEntry,
    WebhookDelivery,
)

__all__ = [
    "ApiCost",
    "AuditLog",
    "Campaign",
    "CampaignProspect",
    "Company",
    "ConsentRecord",
    "Contact",
    "Counter",
    "DemoRequest",
    "DiscoveryJob",
    "ImportBatch",
    "InboundMessage",
    "IndustryPlaybookRow",
    "LLMCall",
    "Mailbox",
    "MailboxPool",
    "MessageEvent",
    "OutboxEvent",
    "OutreachMessage",
    "PageCache",
    "RefreshToken",
    "Reply",
    "ResearchSnapshot",
    "SavedMapping",
    "Setting",
    "SuppressionEntry",
    "Task",
    "Template",
    "User",
    "WebhookDelivery",
]
