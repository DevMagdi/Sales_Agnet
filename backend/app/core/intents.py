"""Deterministic multilingual intent / opt-out rules.

These run independently of (and before) any LLM. An opt-out match here is final: the contact is
suppressed even if the model disagrees. Fail-safe by design — a frequency complaint
("بلاش تبعتوا كتير" = "don't send so much") is treated as an opt-out and flagged for review,
because losing a lead is cheaper than messaging someone who asked us to stop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.text import normalise_text


@dataclass(frozen=True)
class IntentHit:
    label: str
    pattern: str


# Each pattern is matched against normalised text (lowercase, no diacritics, alef/ya/ta unified).
OPT_OUT_PATTERNS: list[tuple[str, str]] = [
    # Arabic stems are matched without a trailing boundary so that inflected forms are caught:
    # أوقفوا / اوقفوا / وقفوا / توقفوا / يوقف. A leading boundary keeps "موقف" (situation) safe.
    ("ar_stop", r"\b(?:ايقاف|إيقاف|وقف|قف|اوقف|أوقف|اوقفوا|وقفوا|توقفوا|يوقف|ايقافوا)"),
    ("ar_do_not_send", r"(?:لا\s+تراسل|لا\s+ترسل|لا\s+تبعت|متبعتوش|متبعتليش|متبعتوليش|متترسلوش|بلاش\s+تبعت|بلاش\s+ترسل|بلاش\s+رسايل|بلاش\s+ايميلات|مش\s+عايزين\s+نستلم|مش\s+عايز\s+استلم|مش\s+عايزين\s+رسايل)"),
    ("ar_remove_me", r"(?:شيلوني|شلوني|امسحوني|اشيلوني|شيلوا\s+اسمي|احذفوني|الغاء\s+الاشتراك|الغوا\s+الاشتراك|الغوا|كفايه|كفاية)"),
    ("ar_no_more", r"(?:مش\s+مهتم|مش\s+مهتمين|غير\s+مهتم|مش\s+عايز\s+رسايل|مفيش\s+لازمه)"),
    ("en_unsubscribe", r"\bunsubscrib(?:e|ing)|opt[\s-]?out\b"),
    ("en_stop", r"\bstop\b(?!\s+(?:by|the|at|over))"),
    ("en_remove", r"\b(?:remove\s+me|take\s+me\s+off|delete\s+me|drop\s+me)\b"),
    ("en_do_not_contact", r"\b(?:do\s+not|don'?t|never)\s+(?:contact|email|message|write)\b"),
    ("en_no_more", r"\b(?:no\s+more\s+(?:emails?|messages?)|not\s+interested\s+in\s+(?:more|further))"),
    ("translit", r"\b(?:batel|batelou|wa2ef|waqef|esh|khalas\s+el\s+rasayel)\b"),
]

# Percentage-complaint phrasing that must be treated as opt-out (fail-safe).
_FREQUENCY_PATTERNS = [
    r"(?:بلاش|متبعتوش?|متبعتليش)\s*(?:تبعتوا|تبعتولي|تراسلوني)?\s*(?:كتير|تاني|تانى)",
    r"(?:رسايل|ايميلات|إيميلات).{0,12}(?:كتير|زياده|زيادة)",
]

INTENT_PATTERNS: dict[str, list[str]] = {
    "meeting_proposal": [
        r"(?:meeting|call|demo|zoom|teams|ميتنج|اجتماع|مكالمه|مكالمة|ديمو|عرض\s+تجريبي)",
        r"(?:ممكن\s+نتكلم|نتقابل|نحدد\s+ميعاد|موعد)",
        r"(?:نفرح\s+نتقابل|available\s+(?:on|at|next))",
    ],
    "wants_info": [
        r"(?:more\s+(?:info|details)|send\s+(?:me\s+)?(?:the\s+)?(?:info|details|brochure)|تفاصيل|معلومات|ابعتلي\s+عرض|ابعتلى\s+عرض)",
        r"(?:how\s+does\s+it\s+work|ازاي\s+بتشتغل|إزاي\s+بتشتغل)",
    ],
    "referral": [
        r"(?:talk\s+to|speak\s+with|contact|forward(?:ed)?\s+to|يرجى\s+التواصل\s+مع|كلم|تواصل\s+مع)",
        r"(?:he\s+is|she\s+is|هو\s+المسؤول|هي\s+المسؤوله)",
    ],
    "not_now": [
        r"(?:not\s+now|later|next\s+(?:quarter|year)|بعدين|مش\s+الوقت|الربع\s+القادم|السنه\s+الجديده)",
    ],
    "not_interested": [
        r"(?:not\s+interested|no\s+thanks|no\s+thank\s+you|غير\s+مهتم|مش\s+مهتمين|لا\s+شكرا|لا\s+شكرًا)",
    ],
    "question_pricing": [
        r"(?:price|pricing|cost|how\s+much|سعر|اسعار|أسعار|تكلفه|تكلفة|كام)",
    ],
    "question_technical": [
        r"(?:integration|api|erp|security|deploy|on[\s-]?prem|تكامل|ربط|امان|أمان|استضافه)",
    ],
    "complaint": [
        r"(?:spam|complaint|report\s+you|شكوى|هاقدم\s+شكوى|بلاغ)",
    ],
    "out_of_office": [
        r"(?:out\s+of\s+(?:office|town)|on\s+(?:annual\s+)?leave|annual\s+leave|vacation|مم\s+absent)",
        r"(?:اجازه|أجازة|خارج\s+المكتب|مسافر)",
    ],
}

_AUTO_REPLY_PATTERNS = [
    r"(?:automatic(?:ally)?\s+generated|auto[\s-]?reply|auto[\s-]?response|do\s+not\s+reply)",
    r"(?:this\s+is\s+an\s+automated|undeliverable|mailbox\s+is\s+full|delivery\s+has\s+failed)",
    r"(?:رد\s+الي\s+تلقائي|رساله\s+تلقائيه|لا\s+ترد\s+علي\s+هذا\s+البريد)",
]


def matches_opt_out(text: str) -> IntentHit | None:
    normalised = normalise_text(text)
    for label, pattern in OPT_OUT_PATTERNS:
        if re.search(pattern, normalised):
            return IntentHit(label=label, pattern=pattern)
    for pattern in _FREQUENCY_PATTERNS:
        if re.search(pattern, normalised):
            return IntentHit(label="frequency_complaint", pattern=pattern)
    return None


def score_intents(text: str) -> dict[str, int]:
    normalised = normalise_text(text)
    scores: dict[str, int] = {}
    for label, patterns in INTENT_PATTERNS.items():
        hits = sum(1 for p in patterns if re.search(p, normalised))
        if hits:
            scores[label] = hits
    return scores


def looks_automated(text: str, headers: dict[str, str] | None = None) -> bool:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if headers.get("auto-submitted", "no").lower() not in ("no", ""):
        return True
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return True
    if headers.get("precedence", "").lower() in ("auto_reply", "bulk", "junk"):
        return True
    subject_hint = headers.get("subject", "")
    normalised = normalise_text(f"{subject_hint} {text}")
    return any(re.search(p, normalised) for p in _AUTO_REPLY_PATTERNS)


def looks_out_of_office(text: str, headers: dict[str, str] | None = None) -> bool:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if "x-autoreply" in headers and any(
        marker in str(headers.get("subject", "")).lower() for marker in ("out of office", "أجازة", "اجازه")
    ):
        return True
    normalised = normalise_text(text)
    ooo = (
        r"(?:out\s+of\s+(?:the\s+)?office|annual\s+leave|on\s+leave|parental\s+leave|"
        r"خارج\s+المكتب|في\s+أجازة|في\s+اجازه|أجازة\s+سنوية|مسافر)"
    )
    return bool(re.search(ooo, normalised))


def detect_bounce(text: str, headers: dict[str, str] | None = None) -> tuple[bool, str]:
    """Return (is_bounce, hard|soft|unknown) from a DSN or provider notice."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    ctype = headers.get("content-type", "").lower()
    normalised = normalise_text(text)
    dsn = "multipart/report" in ctype or "report-type=delivery-status" in ctype or "delivery-status" in normalised
    if not dsn and not re.search(
        r"(?:mailer-daemon|postmaster@|undeliverable|delivery\s+status|mailbox\s+full|quota\s+exceeded|"
        r"could\s+not\s+be\s+delivered|address\s+not\s+found)",
        normalised,
    ):
        return False, "unknown"
    hard_markers = [
        "user unknown",
        "no such user",
        "does not exist",
        "recipient not found",
        "mailbox unavailable",
        "5.1.1",
        "5.1.2",
        "5.4.1",
        "550",
        "551",
        "553",
    ]
    soft_markers = ["mailbox full", "quota exceeded", "4.2.2", "4.7.0", "try again", "temporarily", "452", "450"]
    if any(m in normalised for m in hard_markers):
        return True, "hard"
    if any(m in normalised for m in soft_markers):
        return True, "soft"
    return True, "unknown"
