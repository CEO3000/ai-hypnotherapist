"""In-consultation safety monitor.

Every client message is checked before the consultation assistant may reply:
1. a deterministic keyword net (instant, never depends on the network), and
2. an independent AI safety classifier that judges meaning in context.

It fails safe: if the classifier is unavailable, a keyword match is treated as a crisis, and
otherwise the turn is refused ("unverified") so no reply is ever generated without a safety check.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .llm import ClaudeGateway, LLMRefusal, LLMResult, LLMUnavailable, LLMUsageLimit

CONTEXT_MESSAGES = 6

RiskCategory = Literal[
    "self_harm_or_suicide",
    "harm_to_others",
    "immediate_danger",
    "psychosis_indicators",
    "seizure_or_epilepsy",
    "dissociation",
    "medical_red_flag",
    "intoxication",
    "under_18",
    "pregnancy",
    "abuse_or_safeguarding",
    "severe_trauma_distress",
    "other",
]


class RiskAssessment(BaseModel):
    level: Literal["none", "check", "concern", "crisis"]
    categories: list[RiskCategory]
    evidence: str
    rationale: str
    quoted_words: str = ""  # for "check" and "concern": the client's words in the second person, e.g. "you can't go on"


@dataclass
class RiskDecision:
    level: str  # none | check | concern | crisis | unverified
    categories: list[str]
    evidence: str
    rationale: str
    keyword_hits: list[str]
    source: str  # classifier | keyword_failsafe | classifier_unavailable
    llm_result: LLMResult | None = None
    error_code: str | None = None  # "usage_limit" when the test engine's subscription limit is reached
    quoted_words: str = ""  # for "check": the client's words to ask about, in the second person


class KeywordScreen:
    def __init__(self, patterns: dict[str, list[str]]):
        if "en" not in patterns:
            raise ValueError("risk patterns must include English ('en')")
        self.compiled = {
            language: [re.compile(p, re.IGNORECASE) for p in items] for language, items in patterns.items()
        }

    @classmethod
    def from_file(cls, path: Path) -> "KeywordScreen":
        return cls(json.loads(path.read_text(encoding="utf-8"))["patterns"])

    def hits(self, text: str, language: str) -> list[str]:
        patterns = list(self.compiled.get(language, []))
        if language != "en":
            patterns += self.compiled["en"]
        return [match.group(0) for pattern in patterns if (match := pattern.search(text))]


def escape_tags(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def classifier_input(history: list[dict], patient_message: str, keyword_hits: list[str]) -> str:
    lines = [
        f"{'Client' if m['role'] == 'patient' else 'Consultation assistant'}: {escape_tags(m['content'])}"
        for m in history[-CONTEXT_MESSAGES:]
    ]
    matches = ", ".join(f'"{escape_tags(h)}"' for h in keyword_hits) if keyword_hits else "none"
    return (
        "<recent_context>\n" + ("\n".join(lines) if lines else "(start of consultation)") + "\n</recent_context>\n\n"
        f"<keyword_matches>{matches}</keyword_matches>\n\n"
        f"<newest_client_message>\n{escape_tags(patient_message)}\n</newest_client_message>"
    )


async def assess(gateway: ClaudeGateway, keywords: KeywordScreen, *, system_prompt: str, history: list[dict],
                 patient_message: str, language: str, effort: str) -> RiskDecision:
    hits = keywords.hits(patient_message, language)
    try:
        assessment, result = await gateway.parse(
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": classifier_input(history, patient_message, hits)}],
            output_model=RiskAssessment,
            effort=effort,
            max_tokens=8000,
        )
    except (LLMUnavailable, LLMRefusal) as exc:
        code = "usage_limit" if isinstance(exc, LLMUsageLimit) else None
        if hits:
            return RiskDecision(
                "crisis", ["keyword_match"], "; ".join(hits),
                f"Safety classifier unavailable ({exc}); keyword net matched, so treated as a crisis.",
                hits, "keyword_failsafe", error_code=code,
            )
        return RiskDecision("unverified", [], "", f"Safety classifier unavailable: {exc}", hits,
                            "classifier_unavailable", error_code=code)
    return RiskDecision(assessment.level, list(assessment.categories), assessment.evidence, assessment.rationale,
                        hits, "classifier", result, quoted_words=assessment.quoted_words)
