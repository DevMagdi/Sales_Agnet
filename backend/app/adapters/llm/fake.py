"""Deterministic fake LLM.

Two modes:
  * scripted  — a queue of raw JSON strings per task (used by unit/integration tests and evals)
  * heuristic — derives a schema-valid answer from the machine-readable context embedded in the
                prompt, so `dry_run` and the E2E journeys work end to end with zero network

The fake lives only in dev/test/dry_run; startup validation refuses it in staging/production.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

from app.adapters.base import LLMResult
from app.core.intents import matches_opt_out, score_intents


def _extract_json_block(prompt: str, marker: str = "CONTEXT_JSON:") -> dict[str, Any]:
    idx = prompt.find(marker)
    if idx == -1:
        return {}
    raw = prompt[idx + len(marker) :].strip()
    depth = 0
    start = raw.find("{")
    if start == -1:
        return {}
    for pos in range(start, len(raw)):
        if raw[pos] == "{":
            depth += 1
        elif raw[pos] == "}":
            depth -= 1
            if depth == 0:
                try:
                    block: dict[str, Any] = json.loads(raw[start : pos + 1])
                    return block
                except json.JSONDecodeError:
                    return {}
    return {}


class DeterministicLLM:
    name = "fake"
    is_fake = True

    def __init__(self, scripted: dict[str, list[str]] | None = None) -> None:
        self.scripted: dict[str, list[str]] = {k: list(v) for k, v in (scripted or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def push(self, task: str, payload: dict[str, Any] | str) -> None:
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.scripted.setdefault(task, []).append(raw)

    def healthcheck(self) -> tuple[bool, str]:
        return True, "deterministic fake LLM (dev/test/dry_run only)"

    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        model_tier: Literal["fast", "strong"] = "fast",
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ) -> LLMResult:
        started = time.perf_counter()
        self.calls.append({"task": task, "user": user, "system": system})
        queue = self.scripted.get(task) or []
        text = queue.pop(0) if queue else json.dumps(self._heuristic(task, user), ensure_ascii=False)
        latency = int((time.perf_counter() - started) * 1000)
        return LLMResult(
            text=text,
            model=f"fake-{model_tier}",
            input_tokens=max(1, len(user) // 4),
            output_tokens=max(1, len(text) // 4),
            cost_usd=0.0,
            latency_ms=latency,
            raw={"task": task, "fake": True},
        )

    # ------------------------------------------------------------------ heuristics
    def _heuristic(self, task: str, user: str) -> dict[str, Any]:
        ctx = _extract_json_block(user)
        if task == "research":
            return self._research(ctx)
        if task == "compose_slot":
            return self._compose(ctx)
        if task == "claim_verify":
            return self._claim_ctx(ctx)
        if task == "classify_reply":
            return self._classify(ctx)
        if task == "draft_reply":
            return {"draft": self._draft(ctx)}
        if task == "playbook_select":
            return {"industry": ctx.get("industry_hint") or "unknown", "confidence": 0.7}
        return {}

    @staticmethod
    def _research(ctx: dict[str, Any]) -> dict[str, Any]:
        industry = ctx.get("industry_hint") or "unknown"
        evidence_in = ctx.get("evidence") or []
        evidence = [
            {"id": e.get("id", f"e{i + 1}"), "url": e.get("url", ""), "quote": e.get("quote", "")}
            for i, e in enumerate(evidence_in)
            if e.get("quote")
        ][:4]
        first = evidence[0]["id"] if evidence else ""
        categories = ctx.get("playbook_categories") or []
        likely = []
        if categories and first:
            likely = [
                {
                    "category": str(categories[0]),
                    "rationale": "mentioned activity on the company website",
                    "evidence_id": first,
                    "confidence": 0.6,
                }
            ]
        return {
            "company_summary": f"Company active in {industry}." if industry != "unknown" else "Unknown company.",
            "industry": industry,
            "products": [{"name": str(p), "evidence_id": first} for p in (ctx.get("product_hints") or [])[:3]]
            if first
            else [],
            "likely_purchased_categories": likely,
            "procurement_signals": [{"signal": ctx.get("procurement_signal", "none"), "evidence_id": first}]
            if first
            else [],
            "language": ctx.get("site_language", "unknown"),
            "size_hint": ctx.get("size_hint", "unknown"),
            "named_procurement_contacts": [],
            "do_not_contact_signals": [],
            "evidence": evidence,
        }

    @staticmethod
    def _compose(ctx: dict[str, Any]) -> dict[str, Any]:
        lang = ctx.get("language", "ar")
        category = ctx.get("category") or ("منتجاتكم" if lang == "ar" else "your products")
        evidence_id = ctx.get("evidence_id") or ""
        industry = ctx.get("industry_name") or ""
        if ctx.get("style") == "hedged" or not evidence_id:
            text = (
                f"كثير من شركات {industry} بتشتري {category} عبر عدة موردين في نفس الوقت."
                if lang == "ar"
                else f"Many companies in {industry} buy {category} from several suppliers at once."
            )
            return {"opening_observation": text, "evidence_id": "", "style": "hedged"}
        text = (
            f"لاحظنا من موقعكم أنكم تعملون في {category}."
            if lang == "ar"
            else f"Your website shows that you work with {category}."
        )
        return {"opening_observation": text, "evidence_id": evidence_id, "style": "grounded"}

    @staticmethod
    def _claim_ctx(ctx: dict[str, Any]) -> dict[str, Any]:
        statements = ctx.get("statements") or []
        return {
            "statements": [{"text": s, "supported": True, "source": "template"} for s in statements],
            "verdict": "pass",
        }

    @staticmethod
    def _classify(ctx: dict[str, Any]) -> dict[str, Any]:
        text = str(ctx.get("reply_text", ""))
        lang = ctx.get("language", "ar")
        if matches_opt_out(text):
            return {
                "class": "unsubscribe",
                "confidence": 0.95,
                "possible_opt_out": True,
                "language": lang,
                "extracted": {},
            }
        scores = score_intents(text)
        if not scores:
            return {
                "class": "unclear",
                "confidence": 0.3,
                "possible_opt_out": False,
                "language": lang,
                "extracted": {},
            }
        best = max(scores.items(), key=lambda kv: kv[1])[0]
        mapping = {
            "meeting_proposal": "meeting_proposal",
            "wants_info": "wants_info",
            "referral": "referral",
            "not_now": "not_now",
            "not_interested": "not_interested",
            "question_pricing": "question_pricing",
            "question_technical": "question_technical",
            "complaint": "complaint",
            "out_of_office": "out_of_office",
        }
        extracted: dict[str, Any] = {}
        emails = re.findall(r"[\w.\-+]+@[\w.\-]+\.\w+", text)
        if mapping.get(best) == "referral" and emails:
            extracted["referred_email"] = emails[0]
        return {
            "class": mapping.get(best, "unclear"),
            "confidence": 0.8,
            "possible_opt_out": False,
            "language": lang,
            "extracted": extracted,
        }

    @staticmethod
    def _draft(ctx: dict[str, Any]) -> str:
        lang = ctx.get("language", "ar")
        facts = ctx.get("facts") or []
        bullets = "\n".join(f"- {f}" for f in facts[:3])
        if lang == "ar":
            return f"شكرًا على تواصلكم.\n\n{build_ar_tail()}\n\n{bullets}"
        return f"Thank you for getting back to us.\n\n{build_en_tail()}\n\n{bullets}"


def build_ar_tail() -> str:
    return "ملخص سريع لـ ProcureAI:"


def build_en_tail() -> str:
    return "A quick summary of ProcureAI:"
