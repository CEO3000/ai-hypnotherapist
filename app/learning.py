"""Human-gated learning loop.

Clinician reviews (edits, rejections, notes) and client feedback are distilled into proposed
lessons. A lesson only affects future scripts after a clinician approves it.
"""

import difflib

from pydantic import BaseModel

from .llm import ClaudeGateway, LLMResult
from .risk import escape_tags

MAX_REVIEWS = 20
MAX_DIFF_CHARS = 4000
MAX_LESSONS = 5


class LessonProposal(BaseModel):
    lesson: str
    rationale: str
    evidence_script_ids: list[int]


class LessonProposals(BaseModel):
    lessons: list[LessonProposal]


def review_diff(draft: str, final: str | None) -> str:
    if not final or final == draft:
        return "(no edits)"
    diff = "\n".join(difflib.unified_diff(
        draft.splitlines(), final.splitlines(), "ai_draft", "clinician_final", lineterm="", n=1,
    ))
    return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n... (diff truncated)"


def render_reviews(reviews: list[dict]) -> str:
    blocks = []
    for r in reviews:
        feedback = "none"
        if r.get("rating") is not None or r.get("comments"):
            parts = []
            if r.get("rating") is not None:
                parts.append(f"rating {r['rating']}/5")
            if r.get("severity_after") is not None:
                parts.append(f"issue severity {r.get('baseline_severity')}/10 before, {r['severity_after']}/10 after")
            parts.append(f"comments: {escape_tags(r.get('comments') or '')}")
            feedback = "; ".join(parts)
        blocks.append(
            f'<review script_id="{r["script_id"]}" decision="{r["review_decision"]}">\n'
            f"<clinician_notes>{escape_tags(r.get('review_notes') or '')}</clinician_notes>\n"
            f"<diff>\n{escape_tags(review_diff(r['draft'], r.get('final_text')))}\n</diff>\n"
            f"<client_feedback>{feedback}</client_feedback>\n"
            "</review>"
        )
    return "\n\n".join(blocks)


async def propose_lessons(gateway: ClaudeGateway, *, system_prompt: str, reviews: list[dict],
                          approved: list[dict], effort: str) -> tuple[list[LessonProposal], LLMResult]:
    approved_text = "\n".join(f"- {a['lesson']}" for a in approved) or "(none)"
    user = (
        f"<approved_lessons>\n{escape_tags(approved_text)}\n</approved_lessons>\n\n"
        f"<reviews>\n{render_reviews(reviews)}\n</reviews>"
    )
    proposals, result = await gateway.parse(
        system=[{"type": "text", "text": system_prompt}],
        messages=[{"role": "user", "content": user}],
        output_model=LessonProposals,
        effort=effort,
        max_tokens=16000,
    )
    valid_ids = {r["script_id"] for r in reviews}
    supported = []
    for proposal in proposals.lessons[:MAX_LESSONS]:
        evidence = [i for i in proposal.evidence_script_ids if i in valid_ids]
        if evidence and proposal.lesson.strip():
            supported.append(proposal.model_copy(update={"evidence_script_ids": evidence}))
    return supported, result
