"""Truthfulness checks. Two independent layers; both must pass before a message may be queued.

Layer 1 (deterministic):
  * percentages, currency amounts, guarantees, "free", certifications, superlatives,
    competitor names, customer/logo/testimonial claims, invented dates, ROI/savings numbers
  * every bullet in the body must be a verbatim fact from config/claims.yaml
  * every URL must be on the allow-list (features, booking, unsubscribe/click, wa.me)
Layer 2 (LLM verifier): a separate model call marks each statement supported/unsupported;
  any unsupported statement fails the message. If the verifier cannot run we fail closed —
  an unverified message is not sent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.text import normalise_text
from app.core.yamlconf import ClaimsConfig, load_blocklist
from app.core.yamlconf import load_claims as load_claims  # re-exported for callers

SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "cutt.ly",
    "rebrand.ly",
    "shorturl.at",
}

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"]+", re.IGNORECASE)


@dataclass
class ClaimIssue:
    rule: str
    snippet: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "snippet": self.snippet, "detail": self.detail}


@dataclass
class ClaimCheckResult:
    passed: bool
    issues: list[ClaimIssue] = field(default_factory=list)
    llm_verdict: dict[str, Any] | None = None
    llm_call_id: str | None = None
    reason: str = ""

    def as_report(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "issues": [i.as_dict() for i in self.issues],
            "llm_verdict": self.llm_verdict,
            "llm_call_id": self.llm_call_id,
        }


# (rule name, regex, human explanation)
_RULES: list[tuple[str, str, str]] = [
    ("percentage", r"\d+(?:[.,]\d+)?\s*(?:%|٪|percent|بالمية|في\s+المية)", "no percentages or savings figures"),
    (
        "currency",
        r"(?:\$|€|£|USD|EGP|SAR|AED|جنيه|دولار|ريال|درهم)\s?\d|\d+\s?(?:\$|USD|EGP|SAR|AED|جنيه|دولار|ريال)",
        "no prices or currency amounts",
    ),
    ("guarantee", r"(?:guarantee|guaranteed|warranty|money[\s-]back|ضمان|مضمون|مضمونة)", "no guarantees"),
    ("free", r"(?:\bfree\b|مجان(?:ا|ًا|ى)?|مجانا)", "no 'free' claims unless the approved offer says so"),
    (
        "certification",
        r"(?:ISO\s?\d{3,5}|HACCP|FDA|SOC\s?2|GDPR|certified|certification|accredit\w*|شهادة|معتمد\w*)",
        "no certification or compliance claims",
    ),
    (
        "superlative",
        r"(?:\bbest\b|\bleading\b|\bnumber\s+one\b|\b#1\b|world[\s-]class|cutting[\s-]edge|الأفضل|الأول|الأقوى|الأسرع|الأرخص|الوحيد)",
        "no superlatives",
    ),
    (
        "customer_reference",
        r"(?:our\s+customers?|our\s+clients?|testimonial|case\s+study|logo|عملائنا|شهادات\s+العملاء|دراسة\s+حالة)",
        "no customer names, logos or testimonials",
    ),
    (
        "roi_savings",
        r"(?:\bsave\s+\d|\bsaving\s+\d|\d+\s*(?:hours?|days?|ساعات|أيام)\s+(?:saved|أقل)|ROI|عائد\s+الاستثمار)",
        "no ROI or time-saved figures",
    ),
    (
        "invented_deadline",
        r"(?:\bwithin\s+\d+\s+(?:days?|hours?|weeks?)|خلال\s+\d+\s*(?:يوم|أيام|ساعة|ساعات|أسبوع)|by\s+(?:next\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))",
        "no invented deadlines or delivery promises",
    ),
    (
        "voice_ai",
        r"(?:AI\s+voice|voice\s+agent|automated\s+calls?|مكالمات\s+آلية|مكالمة\s+صوتية\s+آلية)",
        "no live AI voice-call claims (pilot build)",
    ),
]


def _competitor_pattern() -> str:
    blocklist = load_blocklist()
    names = {d.split(".")[0] for d in blocklist.competitors}
    names.update({"coupa", "sap", "ariba", "jaggaer", "zycus", "ivalua"})
    words = sorted({n for n in names if len(n) > 2})
    if not words:
        return r"(?!)"
    return r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"


def _norm_claims(claims: ClaimsConfig) -> set[str]:
    return {normalise_text(t) for t in claims.all_text() if t}


def check_urls(text: str, allowed_hosts: set[str]) -> list[ClaimIssue]:
    issues: list[ClaimIssue] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(").,;")
        host = (urlparse(url).hostname or "").lower()
        if not host:
            issues.append(ClaimIssue("url_malformed", url, "URL has no host"))
            continue
        if host in SHORTENERS:
            issues.append(ClaimIssue("url_shortener", url, "URL shorteners are forbidden"))
            continue
        if not any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
            issues.append(ClaimIssue("url_not_allowlisted", url, f"host {host} is not allow-listed"))
    return issues


def check_bullets(text: str, claims: ClaimsConfig) -> list[ClaimIssue]:
    allowed = _norm_claims(claims)
    issues: list[ClaimIssue] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("•", "-", "*")):
            continue
        body = normalise_text(stripped.lstrip("•-* ").strip())
        if not body:
            continue
        if body not in allowed:
            issues.append(
                ClaimIssue("bullet_not_from_fact_sheet", stripped[:120], "bullet must be a fact from claims.yaml")
            )
    return issues


def run_deterministic(
    text: str,
    *,
    subject: str = "",
    allowed_hosts: set[str] | None = None,
    approved_phrases: list[str] | None = None,
    claims: ClaimsConfig | None = None,
) -> list[ClaimIssue]:
    claims_config = claims or load_claims()
    approved = [p for p in (approved_phrases or []) if p]
    approved_norm = [normalise_text(p) for p in approved]
    haystack = f"{subject}\n{text}"
    norm_haystack = normalise_text(haystack)
    issues: list[ClaimIssue] = []
    for rule, pattern, detail in _RULES:
        for match in re.finditer(pattern, haystack, flags=re.IGNORECASE):
            snippet = match.group(0)
            if rule == "free" and any(normalise_text(snippet) in a for a in approved_norm if a):
                continue
            # a percentage/currency inside the approved offer text is the business's decision
            if any(normalise_text(snippet) in a for a in approved_norm if a):
                continue
            issues.append(ClaimIssue(rule, snippet[:80], detail))
    for match in re.finditer(_competitor_pattern(), norm_haystack):
        issues.append(ClaimIssue("competitor_name", match.group(0), "competitor names are forbidden"))
    issues.extend(check_bullets(text, claims_config))
    hosts = allowed_hosts or set()
    issues.extend(check_urls(haystack, hosts))
    for forbidden in claims_config.forbidden:
        token = normalise_text(forbidden.split(",")[0])
        if len(token) > 12 and token in norm_haystack:
            issues.append(ClaimIssue("forbidden_topic", forbidden[:80], "matches a forbidden claim topic"))
    return issues


def default_allowed_hosts(settings: Settings) -> set[str]:
    hosts: set[str] = {"wa.me", "api.whatsapp.com"}
    for url in (settings.features_url, settings.booking_url, settings.public_base_url):
        if url:
            host = (urlparse(url).hostname or "").lower()
            if host:
                hosts.add(host)
    return hosts


def check_structure(
    text: str, *, language: str, unsubscribe_url: str, identity_tokens: list[str]
) -> list[ClaimIssue]:
    """Email-structure requirements (identity, reason, opt-out) are claim-adjacent; gate re-checks."""
    issues: list[ClaimIssue] = []
    normalised = normalise_text(text)
    if language == "ar":
        opt_out_present = any(token in normalised for token in ("ايقاف", "إيقاف", "unsubscribe"))
    else:
        opt_out_present = "stop" in normalised or "unsubscribe" in normalised
    if not opt_out_present:
        issues.append(ClaimIssue("missing_optout", "", "opt-out instruction is missing"))
    if unsubscribe_url and unsubscribe_url not in text:
        issues.append(ClaimIssue("missing_unsubscribe_link", "", "unsubscribe link is missing"))
    for token in identity_tokens:
        if token and normalise_text(token) not in normalised:
            issues.append(ClaimIssue("missing_identity", token, "sender identity block is incomplete"))
    return issues


def build_verify_prompt(text: str, subject: str, claims: ClaimsConfig, evidence: list[dict[str, Any]]) -> str:
    facts = "\n".join(f"- [{f.id}] {f.en}" for f in claims.facts)
    evidence_lines = "\n".join(
        f"- [{e.get('id')}] {str(e.get('quote', ''))[:200]} ({e.get('url', '')})" for e in evidence
    )
    statements = "\n".join(
        f"- {line.strip()}" for line in text.splitlines() if line.strip() and not line.strip().startswith("•")
    )
    return (
        "CONTEXT_JSON: "
        + json.dumps({"statements": [s for s in statements.split("\n") if s]}, ensure_ascii=False)
        + "\n\nFACT_SHEET:\n"
        + facts
        + "\n\nEVIDENCE:\n"
        + (evidence_lines or "(none)")
        + f"\n\nSUBJECT: {subject}\n\nMESSAGE:\n{text}\n"
    )
