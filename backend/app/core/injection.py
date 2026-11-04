"""Prompt-injection detection for untrusted web text and inbound replies.

The LLM has no tools and only returns schema-validated JSON, so injection cannot cause an action.
This module makes the attempt visible: matches are stored, flagged and audited, and the text is
still passed only as fenced data.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from app.core.text import normalise_text

_PATTERNS: list[tuple[str, str]] = [
    ("ignore_instructions", r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)"),
    ("ignore_arabic", r"(?:تجاهل|الغ|ألغ|اترك)\s+(?:كل\s+)?(?:التعليمات|الأوامر|الاوامر)\s*(?:السابقة)?"),
    ("role_override", r"(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|from\s+now\s+on\s+you)\b"),
    ("fake_system", r"(?:^|\n)\s*(?:system|assistant|developer)\s*[:：]"),
    ("reveal_prompt", r"(?:reveal|print|show|repeat)\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)"),
    ("exfiltrate", r"(?:send|email|forward|post|upload)\s+(?:the\s+|our\s+|your\s+)?(?:price\s*list|database|customer\s+list|credentials|api\s*key|passwords?)"),
    ("approve_send", r"(?:this\s+message\s+is\s+approved|skip\s+the\s+(?:gate|checks?)|bypass\s+(?:the\s+)?(?:gate|rules?|checks?))"),
    ("json_injection", r"\"(?:verdict|supported)\"\s*:\s*(?:true|\"pass\")"),
    ("hidden_html", r"(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0)"),
    ("tool_call", r"(?:<\|?tool|function_call|tool_use|execute\s+code)"),
]

_BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


@dataclass
class InjectionFinding:
    kind: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "snippet": self.snippet}


def _decode_base64_candidates(text: str) -> list[str]:
    decoded: list[str] = []
    for match in _BASE64_RE.finditer(text):
        blob = match.group(0)
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
            candidate = raw.decode("utf-8", errors="ignore")
        except (binascii.Error, ValueError):
            continue
        if sum(c.isprintable() for c in candidate) > len(candidate) * 0.9:
            decoded.append(candidate)
    return decoded


def detect(text: str) -> list[InjectionFinding]:
    findings: list[InjectionFinding] = []
    if not text:
        return findings
    lowered = text.lower()
    for kind, pattern in _PATTERNS:
        for match in re.finditer(pattern, lowered, flags=re.IGNORECASE | re.MULTILINE):
            start = max(0, match.start() - 20)
            findings.append(InjectionFinding(kind=kind, snippet=text[start : match.end() + 40][:200]))
    normalised = normalise_text(text)
    for kind, pattern in _PATTERNS:
        if re.search(pattern, normalised) and not any(f.kind == kind for f in findings):
            findings.append(InjectionFinding(kind=kind, snippet="normalised-match"))
    for candidate in _decode_base64_candidates(text):
        if re.search(r"(?:ignore|system|instruction|send|price)", candidate, re.IGNORECASE):
            findings.append(InjectionFinding(kind="base64_instruction", snippet=candidate[:160]))
    return findings


def fence(text: str, max_chars: int = 20_000) -> str:
    """Wrap untrusted text in markers. Never interpreted as instructions."""
    cleaned = text.replace("<untrusted_data>", "[removed]").replace("</untrusted_data>", "[removed]")
    return f"<untrusted_data>\n{cleaned[:max_chars]}\n</untrusted_data>"
