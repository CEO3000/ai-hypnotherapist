from app.voice_cues import describe

BASE = {"duration_s": 8.0, "speech_ratio": 0.7, "loudness_db": -30.0, "pitch_hz": 110.0, "pitch_variation": 0.15,
        "pause_count": 1, "longest_pause_s": 0.8, "words_per_minute": 140.0}


def test_too_short_answers_are_not_judged():
    assert describe({**BASE, "duration_s": 1.0}, []) == ""


def test_first_answer_uses_only_absolute_cues():
    assert describe(BASE, []) == ""
    text = describe({**BASE, "words_per_minute": 85, "pitch_variation": 0.04, "pause_count": 3}, [])
    assert "slow pace (85 words/min)" in text
    assert "flat, monotone delivery" in text
    assert "hesitant: 3 pauses" in text
    assert "quieter" not in text  # loudness depends on the microphone, so it is only judged relatively


def test_changes_are_judged_against_the_clients_own_voice():
    earlier = [BASE, {**BASE, "loudness_db": -32.0}]
    text = describe({**BASE, "loudness_db": -37.0, "pitch_hz": 128.0, "words_per_minute": 175.0}, earlier)
    assert "quieter than their earlier answers" in text
    assert "higher-pitched than their earlier answers" in text
    assert "faster than their earlier answers" in text
    assert text.endswith(".")


def test_no_change_is_reported_as_such():
    assert describe(BASE, [BASE]) == "Voice cues (measured automatically): no notable change from their usual voice."


def test_missing_pitch_is_ignored():
    text = describe({**BASE, "pitch_hz": 0, "pitch_variation": 0}, [BASE])
    assert "pitch" not in text and "monotone" not in text
