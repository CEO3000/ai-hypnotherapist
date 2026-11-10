"""Deterministic pre-consultation screening. No AI is involved in these decisions."""

import json
from dataclasses import dataclass
from pathlib import Path

OUTCOMES = ("crisis", "decline_refer", "ineligible", "not_now", "human_only", "clear")


class ScreeningError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    kind: str
    trigger_answer: bool
    outcome: str
    text: dict[str, str]


@dataclass(frozen=True)
class ScreeningResult:
    outcome: str
    matched: list[str]

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "matched": self.matched}


class Screening:
    def __init__(self, data: dict):
        self.version = data["version"]
        self.precedence = data["outcome_precedence"]
        if sorted(self.precedence) != sorted(OUTCOMES):
            raise ScreeningError(f"outcome_precedence must list exactly {OUTCOMES}")
        self.questions: list[Question] = []
        seen = set()
        for raw in data["questions"]:
            question = Question(raw["id"], raw["kind"], raw["trigger_answer"], raw["outcome"], raw["text"])
            if question.id in seen:
                raise ScreeningError(f"duplicate question id {question.id}")
            if question.kind not in ("question", "consent"):
                raise ScreeningError(f"{question.id}: kind must be 'question' or 'consent'")
            if not isinstance(question.trigger_answer, bool):
                raise ScreeningError(f"{question.id}: trigger_answer must be true or false")
            if question.outcome not in OUTCOMES or question.outcome == "clear":
                raise ScreeningError(f"{question.id}: invalid outcome {question.outcome!r}")
            if "en" not in question.text:
                raise ScreeningError(f"{question.id}: missing English text")
            seen.add(question.id)
            self.questions.append(question)

    @classmethod
    def from_file(cls, path: Path) -> "Screening":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def questions_for(self, language: str) -> list[dict]:
        return [
            {"id": q.id, "kind": q.kind, "text": q.text.get(language, q.text["en"])}
            for q in self.questions
        ]

    def evaluate(self, answers: dict) -> ScreeningResult:
        expected = {q.id for q in self.questions}
        missing = expected - answers.keys()
        unknown = answers.keys() - expected
        if missing:
            raise ScreeningError(f"unanswered screening questions: {sorted(missing)}")
        if unknown:
            raise ScreeningError(f"unknown screening questions: {sorted(unknown)}")
        for qid, value in answers.items():
            if not isinstance(value, bool):
                raise ScreeningError(f"answer to {qid} must be true or false")

        matched = [q for q in self.questions if answers[q.id] == q.trigger_answer]
        if not matched:
            return ScreeningResult("clear", [])
        outcome = min((q.outcome for q in matched), key=self.precedence.index)
        return ScreeningResult(outcome, [q.id for q in matched])
