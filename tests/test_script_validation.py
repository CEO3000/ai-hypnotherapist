from app.script_writer import patient_version, retrieval_query, spoken_text, validate_script
from tests.support import valid_script


def test_valid_script_passes():
    assert validate_script(valid_script(), category="smoking-cessation", stop_reason="end_turn") == []


def test_truncation_is_flagged():
    issues = validate_script(valid_script(), category="smoking-cessation", stop_reason="max_tokens")
    assert "cut off" in issues[0]


def test_missing_and_misordered_sections_are_flagged():
    script = valid_script().replace("## DEEPENER", "## SOMETHING ELSE")
    assert "Missing section: DEEPENER" in validate_script(script, category="other", stop_reason="end_turn")
    parts = valid_script().split("\n\n")
    swapped = "\n\n".join(parts[:3] + [parts[4], parts[3]] + parts[5:])
    assert "Sections are not in the required order" in validate_script(swapped, category="other",
                                                                        stop_reason="end_turn")


def test_safety_lines_are_required():
    no_net = valid_script().replace("you can open your eyes and be fully alert immediately", "carry on")
    assert any("safety-net" in i for i in validate_script(no_net, category="other", stop_reason="end_turn"))
    no_driving = valid_script().replace("never while driving", "whenever")
    assert any("driving" in i for i in validate_script(no_driving, category="other", stop_reason="end_turn"))


def test_emergence_must_end_alert_except_for_sleep_sessions():
    drowsy = valid_script().replace("eyes open, fully alert and refreshed", "drifting into deep sleep")
    assert any("Emergence" in i for i in validate_script(drowsy, category="other", stop_reason="end_turn"))
    assert validate_script(drowsy, category="sleep-and-insomnia", stop_reason="end_turn") == []


def test_flagged_content_is_reported():
    touch = valid_script().replace("## INDUCTION\n", "## INDUCTION\nNow I will touch your forehead. ")
    assert any("touch" in i for i in validate_script(touch, category="other", stop_reason="end_turn"))
    strobe = valid_script().replace("## INDUCTION\n", "## INDUCTION\nWatch the flashing light. ")
    assert any("strobe" in i for i in validate_script(strobe, category="other", stop_reason="end_turn"))


def test_length_limits():
    short = valid_script(words_per_section=40)
    assert any("short" in i for i in validate_script(short, category="other", stop_reason="end_turn"))


def test_clinician_notes_and_delivery_notes_are_not_spoken():
    spoken = spoken_text(valid_script())
    assert "Not read aloud" not in spoken
    assert "slow down here" not in spoken
    assert "PART 1" not in spoken
    view = patient_version(valid_script())
    assert view.startswith("# Smoking Self-Guided Hypnotherapy Session")
    assert "CLINICIAN NOTES" not in view and "coffee-break origin moment" not in view


def test_retrieval_query_uses_intake_themes():
    query = retrieval_query({
        "presenting_issue": "smoking", "presenting_issue_category": "smoking-cessation",
        "goal_in_client_words": "be free", "knowledge_search_terms": ["stress", "habit"],
        "current_triggers": [{"situation": "coffee break", "client_words": "x"}],
        "origin_moments": [{"feeling": "left out", "belief_formed": "smoking makes me belong"}],
    })
    for word in ("smoking cessation", "stress", "coffee break", "belong"):
        assert word in query
