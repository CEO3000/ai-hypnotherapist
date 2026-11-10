"""One consultant turn as structured output: the latest answer is analysed first, then the reply is written from
that analysis (field order makes the model produce the analysis before the reply). The analysis is kept to short
phrases so a turn stays quick."""

from typing import Literal

from pydantic import BaseModel, Field

Delivery = Literal["warm", "gentle", "reassuring", "curious", "encouraging", "serious"]


class AnswerAnalysis(BaseModel):
    facts: list[str] = Field(description="Concrete facts in the latest answer: short phrases, at most 4.")
    feelings: list[str] = Field(
        description="Emotions expressed or implied, including what any voice cues suggest: short phrases, at most 3.")
    beliefs_or_conclusions: list[str] = Field(
        description="Conclusions the client drew, in their own words where possible: short phrases, at most 3.")
    links_to_earlier_answers: list[str] = Field(description="Connections to earlier answers: short phrases, at most 2.")
    inconsistencies: list[str] = Field(description="Conflicts with earlier answers: short phrases; empty if none.")
    gaps: list[str] = Field(description="What is vague or missing that matters: short phrases, at most 3.")
    next_focus: str = Field(description="One short sentence: the thread to follow next, and why.")


class ConsultantTurn(BaseModel):
    analysis: AnswerAnalysis
    delivery: Delivery = Field(description="The tone of voice for this reply, matched to how the client seems to feel.")
    reply: str = Field(description="Your next message to the client, following every style rule.")
    consultation_complete: bool
    coverage_note: str = Field(
        description="Only when the consultation is complete: one or two sentences for the clinician on what was "
                    "covered well and what is missing. Otherwise empty.")
