"""Turns a finished consultation transcript into a structured intake record."""

from typing import Literal

from pydantic import BaseModel, Field

from .knowledge import PRESENTING_ISSUES
from .llm import ClaudeGateway, LLMResult
from .risk import escape_tags

IssueCategory = Literal[PRESENTING_ISSUES + ("other",)]
# The practice's rule: the qualified hypnotherapist works with these himself (they need regression work, which a
# recorded session cannot do, or his own care in pregnancy). Everything else (depression, stress, anxiety,
# insomnia, habits, addictions...) is for the AI session; safety reasons go to a doctor instead (Suitability).
HypnotherapistIssue = Literal["none", "ptsd", "phobia", "stuttering", "migraine", "panic_attacks", "pregnancy"]


class OriginMoment(BaseModel):
    description: str = Field(description="What happened, in the client's own words as far as possible.")
    approximate_age_or_time: str
    feeling: str
    belief_formed: str = Field(description="The conclusion the client drew, in their own causal language; empty if not stated.")
    client_quotes: list[str]
    consistency: Literal["consistent", "clarified", "unresolved_contradiction"] = Field(
        description="Whether the client's account of this moment stayed consistent throughout the consultation.")


class Inconsistency(BaseModel):
    description: str = Field(description="What does not fit together, stated neutrally.")
    earlier_statement: str = Field(description="The client's words the first time.")
    later_statement: str = Field(description="The client's words that conflict with them.")
    clarified: bool
    clarification: str = Field(description="How the client explained it; empty if not clarified.")


class Trigger(BaseModel):
    situation: str
    client_words: str


class Suitability(BaseModel):
    suitable: bool
    reasons: list[str]


class ConsultationIntake(BaseModel):
    preferred_name: str
    presenting_issue: str
    presenting_issue_category: IssueCategory
    goal_in_client_words: str
    history_summary: str
    origin_moments: list[OriginMoment]
    current_triggers: list[Trigger]
    impact_on_life: str
    previously_tried: str
    motivation_now: str
    resources_and_strengths: list[str]
    imagery_preferences: list[str]
    imagery_to_avoid: list[str]
    previous_hypnosis_experience: str
    hypnosis_concerns: list[str]
    key_client_phrases: list[str]
    knowledge_search_terms: list[str]
    consistency_review: list[Inconsistency]
    reliability_note: str = Field(
        description="A brief, neutral note for the clinician on how consistent and specific the account was.")
    hypnotherapist_issue: HypnotherapistIssue = Field(
        description="ptsd, phobia, stuttering, migraine, panic_attacks or pregnancy when that is the client's "
                    "main issue, a central part of it, or (pregnancy) their situation; otherwise none.")
    suitability_for_self_guided_session: Suitability
    gaps_or_uncertainties: list[str]
    presenting_issue_for_client: str = Field(
        default="", description="presenting_issue in the language of the consultation, shown to the client.")
    goal_for_client: str = Field(
        default="", description="goal_in_client_words in the language of the consultation, shown to the client.")


def format_transcript(messages: list[dict]) -> str:
    return "\n\n".join(
        f"{'Client' if m['role'] == 'patient' else 'Consultation assistant'}: {escape_tags(m['content'])}"
        for m in messages
    )


async def extract_intake(gateway: ClaudeGateway, *, system_prompt: str, messages: list[dict],
                         coverage_note: str | None, effort: str) -> tuple[ConsultationIntake, LLMResult]:
    content = (
        f"<consultation_transcript>\n{format_transcript(messages)}\n</consultation_transcript>\n\n"
        f"<coverage_note>{escape_tags(coverage_note or 'none')}</coverage_note>"
    )
    return await gateway.parse(
        system=[{"type": "text", "text": system_prompt}],
        messages=[{"role": "user", "content": content}],
        output_model=ConsultationIntake,
        effort=effort,
        max_tokens=16000,
    )
