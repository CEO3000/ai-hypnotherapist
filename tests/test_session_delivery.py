from app.script_writer import patient_version
from app.session_delivery import (HYPNOTIC, LONG_PAUSE_MS, NATURAL, PARAGRAPH_PAUSE_MS, SHORT_PAUSE_MS, SLOWER,
                                  delivery_plan, rate_for)
from app.speech import speakable

SCRIPT = """# Exam Nerves Self-Guided Hypnotherapy Session for Sam

## CLINICIAN NOTES
Worked on the school exam memory. Not read aloud.

## PRE-TALK
Welcome to your session, Sam. Listen only when you can relax safely.
> warm and unhurried

## INDUCTION
Take a breath in... and a long, easy breath out. Let your eyes close, and let your shoulders soften now.

## DEEPENER
Ten, going down, nine, deeper and deeper. (pause) That's it.

## PART 1 - The Lantern Room: Belief Archaeology & Transformation
You can see the younger you, sitting at that desk, and you can notice what she needed.

## PART 3 - Future Pacing
Imagine the next exam, calm and clear, knowing exactly what to do.

## PART 4 - Post-Hypnotic Suggestions & Anchors
Each time you pick up your pen, you feel calm, steady and focused.

## EMERGENCE
In a moment I will count from one to five. One, coming back up. Two, feeling lighter. Five, eyes open, wide awake.
"""


def by_text(plan):
    return {item["text"]: item for item in plan}


def test_each_part_of_the_session_has_its_own_delivery():
    plan = by_text(delivery_plan(SCRIPT))
    assert plan["Welcome to your session, Sam."]["style"] == NATURAL
    assert plan["Let your eyes close,"]["style"] == HYPNOTIC
    assert plan["That's it."]["style"] == HYPNOTIC
    assert plan["You can see the younger you, sitting at that desk, and you can notice what she needed."]["style"] == SLOWER
    assert plan["Imagine the next exam, calm and clear, knowing exactly what to do."]["style"] == SLOWER
    assert plan["Each time you pick up your pen,"]["style"] == HYPNOTIC
    emergence = [item for item in delivery_plan(SCRIPT) if item["text"].startswith(("In a moment", "One", "Two", "Five"))]
    assert [item["style"] for item in emergence] == [SLOWER, SLOWER, NATURAL, NATURAL]  # coming back up


def test_the_hypnotic_parts_are_spoken_phrase_by_phrase_without_splitting_short_phrases():
    texts = [item["text"] for item in delivery_plan(SCRIPT)]
    assert "and a long, easy breath out." in texts  # "a long," is too short to stand alone
    assert "Ten, going down, nine, deeper and deeper." in texts
    assert ["Let your eyes close,", "and let your shoulders soften now."] == texts[texts.index("Let your eyes close,"):][:2]
    assert "You can see the younger you, sitting at that desk, and you can notice what she needed." in texts  # slower


def test_pauses_follow_the_script_and_the_style():
    plan = by_text(delivery_plan(SCRIPT))
    assert plan["Take a breath in"]["pause_ms"] >= SHORT_PAUSE_MS  # "..."
    assert plan["Ten, going down, nine, deeper and deeper."]["pause_ms"] >= LONG_PAUSE_MS  # "(pause)"
    assert plan["Let your eyes close,"]["pause_ms"] == 650  # between phrases
    assert plan["Welcome to your session, Sam."]["pause_ms"] == 700  # between natural sentences
    assert plan["Listen only when you can relax safely."]["pause_ms"] == 700 + PARAGRAPH_PAUSE_MS
    assert delivery_plan(SCRIPT)[-1]["pause_ms"] == 0


def test_only_what_is_read_aloud_is_in_the_plan_and_all_of_it_can_be_spoken():
    plan = delivery_plan(SCRIPT)
    joined = " ".join(item["text"] for item in plan)
    for hidden in ("Worked on the school", "warm and unhurried", "Exam Nerves", "PRE-TALK", "Belief Archaeology"):
        assert hidden not in joined
    assert all(speakable(item["text"], [patient_version(SCRIPT)]) for item in plan)


def test_each_style_has_its_pace():
    assert rate_for(NATURAL, 0.8) == 1.0  # the voice's own pace
    assert rate_for(SLOWER, 0.8) == 0.8  # the clinician's session speed
    assert abs(rate_for(HYPNOTIC, 0.8) - 0.752) < 1e-9  # a little slower still
