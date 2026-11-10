"""Turns voice measurements taken in the client's browser into a short, tentative description for the consultant.

Only numbers come from the browser; all wording is produced here, so nothing a client sends can become an
instruction to the model. Changes are judged against the client's own earlier answers, because loudness and pitch
depend on the microphone and the person.
"""

from statistics import mean

MIN_DURATION_S = 1.5


def _baseline(earlier: list[dict], key: str) -> float | None:
    values = [e[key] for e in earlier if e.get(key)]
    return mean(values) if values else None


def describe(current: dict, earlier: list[dict]) -> str:
    if current.get("duration_s", 0) < MIN_DURATION_S:
        return ""
    notes = []

    wpm = current.get("words_per_minute") or 0
    base_wpm = _baseline(earlier, "words_per_minute")
    if wpm and base_wpm:
        if wpm < base_wpm * 0.8:
            notes.append(f"slower than their earlier answers ({wpm:.0f} words/min)")
        elif wpm > base_wpm * 1.2:
            notes.append(f"faster than their earlier answers ({wpm:.0f} words/min)")
    elif wpm:
        if wpm < 100:
            notes.append(f"slow pace ({wpm:.0f} words/min)")
        elif wpm > 175:
            notes.append(f"fast pace ({wpm:.0f} words/min)")

    base_loudness = _baseline(earlier, "loudness_db")
    loudness = current.get("loudness_db")
    if base_loudness is not None and loudness:
        if loudness < base_loudness - 4:
            notes.append("quieter than their earlier answers")
        elif loudness > base_loudness + 4:
            notes.append("louder than their earlier answers")

    base_pitch = _baseline(earlier, "pitch_hz")
    pitch = current.get("pitch_hz") or 0
    if base_pitch and pitch:
        if pitch > base_pitch * 1.12:
            notes.append("higher-pitched than their earlier answers")
        elif pitch < base_pitch * 0.9:
            notes.append("lower-pitched than their earlier answers")

    variation = current.get("pitch_variation")
    if pitch and variation is not None:
        if variation < 0.07:
            notes.append("flat, monotone delivery")
        elif variation > 0.3:
            notes.append("very animated delivery")

    pauses = int(current.get("pause_count") or 0)
    longest = current.get("longest_pause_s") or 0
    if pauses >= 3 or longest >= 2.5:
        notes.append(f"hesitant: {pauses} pauses, the longest {longest:.1f} s")

    if not notes:
        return "Voice cues (measured automatically): no notable change from their usual voice." if earlier else ""
    return "Voice cues (measured automatically, tentative): " + "; ".join(notes) + "."
