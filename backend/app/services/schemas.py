"""Pydantic schemas for every LLM output. Validation is the contract with the model."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

IndustryCode = str


class EvidenceItem(BaseModel):
    id: str
    url: str = ""
    quote: str = ""

    @field_validator("quote")
    @classmethod
    def _short_quote(cls, v: str) -> str:
        words = v.split()
        return " ".join(words[:25])


class ProductItem(BaseModel):
    name: str
    evidence_id: str


class PurchasedCategory(BaseModel):
    category: str
    rationale: str = ""
    evidence_id: str = ""
    confidence: float = 0.0


class ProcurementSignal(BaseModel):
    signal: Literal[
        "supplier_registration_page",
        "erp_mention",
        "multiple_plants",
        "imports",
        "tender_mentions",
        "none",
    ]
    evidence_id: str = ""


class NamedContact(BaseModel):
    name: str
    role: str = ""
    evidence_id: str = ""


class ResearchExtraction(BaseModel):
    company_summary: str = "unknown"
    industry: IndustryCode = "unknown"
    products: list[ProductItem] = Field(default_factory=list)
    likely_purchased_categories: list[PurchasedCategory] = Field(default_factory=list)
    procurement_signals: list[ProcurementSignal] = Field(default_factory=list)
    language: Literal["ar", "en", "mixed", "unknown"] = "unknown"
    size_hint: Literal["micro", "small", "medium", "large", "unknown"] = "unknown"
    named_procurement_contacts: list[NamedContact] = Field(default_factory=list)
    do_not_contact_signals: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    def all_evidence_ids(self) -> set[str]:
        ids = {e.id for e in self.evidence}
        for item in self.products:
            ids.add(item.evidence_id)
        for category in self.likely_purchased_categories:
            ids.add(category.evidence_id)
        for signal in self.procurement_signals:
            ids.add(signal.evidence_id)
        for contact in self.named_procurement_contacts:
            ids.add(contact.evidence_id)
        ids.discard("")
        return ids


class OpeningLine(BaseModel):
    opening_observation: str
    evidence_id: str = ""
    style: Literal["grounded", "hedged"] = "grounded"

    @field_validator("opening_observation")
    @classmethod
    def _limits(cls, v: str) -> str:
        text = " ".join(v.split())
        if len(text.split()) > 25:
            raise ValueError("opening_observation must be <= 25 words")
        if text.count("!") > 0:
            raise ValueError("opening_observation must not contain exclamation marks")
        return text


class ClaimStatement(BaseModel):
    text: str
    supported: bool
    source: Literal["fact_sheet", "evidence", "template", "none"] = "none"


class ClaimVerification(BaseModel):
    statements: list[ClaimStatement] = Field(default_factory=list)
    verdict: Literal["pass", "fail"] = "fail"


class ReplyExtraction(BaseModel):
    requested_times: list[str] = Field(default_factory=list)
    referred_person: str = ""
    referred_email: str = ""
    return_date: str = ""
    questions: list[str] = Field(default_factory=list)


class ReplyClassification(BaseModel):
    class_: str = Field(alias="class", default="unclear")
    confidence: float = 0.0
    possible_opt_out: bool = False
    language: Literal["ar", "en", "mixed"] = "ar"
    extracted: ReplyExtraction = Field(default_factory=ReplyExtraction)

    model_config = {"populate_by_name": True}

    @field_validator("class_")
    @classmethod
    def _known_class(cls, v: str) -> str:
        allowed = {
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
        }
        return v if v in allowed else "unclear"


class DraftedReply(BaseModel):
    draft: str

    @field_validator("draft")
    @classmethod
    def _limit(cls, v: str) -> str:
        if len(v.split()) > 140:
            raise ValueError("draft reply must be <= 140 words")
        return v.strip()


class PlaybookSelection(BaseModel):
    industry: str = "unknown"
    confidence: float = 0.0
