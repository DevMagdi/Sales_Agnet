"""Text normalisation and PII masking helpers used across matching, storage and logging."""

from __future__ import annotations

import re
import unicodedata

ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"
EASTERN_ARABIC_INDIC = "۰۱۲۳۴۵۶۷۸۹"
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate(ARABIC_INDIC)}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate(EASTERN_ARABIC_INDIC)})
_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED\u0640]")
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def latin_digits(text: str) -> str:
    return text.translate(_DIGIT_MAP)


def strip_diacritics(text: str) -> str:
    return _DIACRITICS.sub("", text)


def normalise_space(text: str) -> str:
    return _WS.sub(" ", text).strip()


def normalise_text(text: str) -> str:
    """Normalisation used for evidence-quote substring checks."""
    text = unicodedata.normalize("NFKC", text)
    text = latin_digits(text)
    text = strip_diacritics(text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ى", "ي").replace("ة", "ه")
    return normalise_space(text).lower()


def normalise_company_name(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation, for fuzzy dedupe."""
    text = normalise_text(name)
    for suffix in (
        "ش.م.م",
        "ش م م",
        "ذ.م.م",
        "شركة",
        "مصنع",
        "company",
        "co.",
        "ltd",
        "llc",
        "inc",
        "s.a.e",
        "sae",
        "for",
    ):
        text = text.replace(suffix, " ")
    text = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text)
    return normalise_space(text)


def normalise_email(email: str) -> str:
    return email.strip().lower()


def email_match_key(email: str) -> str:
    """Key used for suppression/dedupe: lowercase, E.164-ish, plus-tag stripped.

    `a.b+lists@gmail.com` and `A.B@Gmail.com` map to the same key so an opt-out can
    never be dodged by re-importing with a plus tag.
    """
    normalised = normalise_email(email)
    if "@" not in normalised:
        return normalised
    local, _, domain = normalised.partition("@")
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def domain_of_email(email: str) -> str:
    return normalise_email(email).partition("@")[2]


def normalise_phone(phone: str, default_region: str = "EG") -> str:
    """Best-effort E.164. Falls back to digits-only when phonenumbers cannot decide."""
    raw = latin_digits(phone).strip()
    try:
        import phonenumbers

        parsed = phonenumbers.parse(raw, default_region)
        if phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:  # noqa: BLE001 - parsing must never raise into the pipeline
        pass
    digits = re.sub(r"[^\d+]", "", raw)
    return digits


def mask_email(email: str) -> str:
    if not email:
        return ""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    keep = local[:2] if len(local) > 2 else local[:1]
    return f"{keep}***@{domain}"


def mask_phone(phone: str) -> str:
    if not phone:
        return ""
    return f"***{phone[-4:]}" if len(phone) > 4 else "***"


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    return f"••••{secret[-4:]}" if len(secret) >= 4 else "••••"


def strip_html(html: str) -> str:
    return normalise_space(_TAG.sub(" ", html))


def extract_emails(text: str) -> list[str]:
    return sorted({m.group(0).strip().lower() for m in _EMAIL_RE.finditer(text)})


def contains_arabic(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06FF" for ch in text)


def word_count(text: str) -> int:
    return len(re.findall(r"[\w\u0600-\u06FF']+", text))


def detect_language(text: str) -> str:
    """ar | en | mixed | unknown. Deterministic, no network."""
    arabic = sum(1 for ch in text if "\u0600" <= ch <= "\u06FF")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    total = arabic + latin
    if total == 0:
        return "unknown"
    ratio = arabic / total
    if ratio > 0.75:
        return "ar"
    if ratio < 0.1:
        return "en"
    return "mixed"


def first_sentence(text: str) -> str:
    parts = re.split(r"(?<=[.!؟?])\s+", normalise_space(text))
    return parts[0] if parts else ""
