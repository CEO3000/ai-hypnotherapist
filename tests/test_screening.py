import copy
import json

import pytest

from app.config import CONTENT_DIR
from app.screening import Screening, ScreeningError

DATA = json.loads((CONTENT_DIR / "screening.json").read_text(encoding="utf-8"))


def all_clear() -> dict:
    return {q["id"]: not q["trigger_answer"] for q in DATA["questions"]}


@pytest.fixture
def screening():
    return Screening(DATA)


def test_all_clear_passes(screening):
    result = screening.evaluate(all_clear())
    assert result.outcome == "clear"
    assert result.matched == []


@pytest.mark.parametrize("question_id, expected", [
    ("suicidal_thoughts", "crisis"),
    ("self_harm_recent", "crisis"),
    ("epilepsy", "decline_refer"),
    ("psychosis_bipolar", "decline_refer"),
    ("hallucinations_now", "decline_refer"),
    ("dissociative_disorder", "decline_refer"),
    ("heart_condition", "decline_refer"),
    ("unchecked_symptom", "decline_refer"),
    ("pregnant", "human_only"),
    ("intoxicated", "not_now"),
    ("age_18_plus", "ineligible"),
    ("consent_process", "ineligible"),
    ("consent_data", "ineligible"),
])
def test_each_rule_triggers_its_outcome(screening, question_id, expected):
    answers = all_clear()
    answers[question_id] = not answers[question_id]
    result = screening.evaluate(answers)
    assert result.outcome == expected
    assert result.matched == [question_id]


def test_crisis_takes_precedence_over_everything(screening):
    answers = {q["id"]: q["trigger_answer"] for q in DATA["questions"]}
    result = screening.evaluate(answers)
    assert result.outcome == "crisis"
    assert set(result.matched) == {q["id"] for q in DATA["questions"]}


def test_decline_beats_ineligible_and_human_only(screening):
    answers = all_clear()
    answers["pregnant"] = True
    answers["age_18_plus"] = False
    answers["epilepsy"] = True
    assert screening.evaluate(answers).outcome == "decline_refer"


def test_missing_unknown_and_non_boolean_answers_are_rejected(screening):
    answers = all_clear()
    del answers["epilepsy"]
    with pytest.raises(ScreeningError, match="unanswered"):
        screening.evaluate(answers)
    with pytest.raises(ScreeningError, match="unknown"):
        screening.evaluate({**all_clear(), "made_up": True})
    with pytest.raises(ScreeningError, match="true or false"):
        screening.evaluate({**all_clear(), "epilepsy": "no"})


def test_invalid_configuration_is_rejected():
    broken = copy.deepcopy(DATA)
    broken["questions"][0]["outcome"] = "clear"
    with pytest.raises(ScreeningError):
        Screening(broken)
    duplicated = copy.deepcopy(DATA)
    duplicated["questions"].append(duplicated["questions"][0])
    with pytest.raises(ScreeningError, match="duplicate"):
        Screening(duplicated)


def test_every_question_has_english_text_and_a_rationale():
    for q in DATA["questions"]:
        assert q["text"]["en"].strip()
        assert q["rationale"].strip()
