"""How a session is delivered, part by part, the way a hypnotherapist speaks: natural in the pre-talk, slower in
the therapeutic work, a slow hypnotic cadence in the induction, deepener and post-hypnotic suggestions (phrase by
phrase, each phrase ending on its own natural fall, with longer silences), and back to a natural pace as the
client is brought up. No piece starts or stops with a hard edge: each one rises gently into its first word and
settles smoothly after its last (short enough not to sound like a fade). The voice itself is never altered: only its
pace (Signalsmith Stretch, pitch unchanged), those soft starts and ends, the phrasing and the silences."""

import re
from dataclasses import dataclass

from .speech import spoken_form

SHORT_PAUSE_MS = 1200  # "..." in a script
LONG_PAUSE_MS = 3500  # "(pause)" in a script
PARAGRAPH_PAUSE_MS = 1800
SOFT_START_MS = 90  # the voice rises into its first word: no sudden start, not heard as a fade-in
SOFT_END_MS = 220  # and settles after its last word: a smooth ending, never cut off
MIN_PHRASE_WORDS = 4  # shorter pieces ("Now," "a long,") join the next phrase instead of standing alone


@dataclass(frozen=True)
class Style:
    tempo: float  # relative to the clinician's session speed (1.0 = that speed); None = the voice's natural pace
    by_phrase: bool
    phrase_pause_ms: int
    sentence_pause_ms: int
    fade_in_ms: int  # the soft start where the voice begins
    fade_out_ms: int  # and the smooth end where it stops


NATURAL = "natural"
SLOWER = "slower"
HYPNOTIC = "hypnotic"
STYLES = {
    NATURAL: Style(tempo=None, by_phrase=False, phrase_pause_ms=0, sentence_pause_ms=700,
                   fade_in_ms=SOFT_START_MS, fade_out_ms=SOFT_END_MS),
    SLOWER: Style(tempo=1.0, by_phrase=False, phrase_pause_ms=0, sentence_pause_ms=1100,
                  fade_in_ms=SOFT_START_MS, fade_out_ms=SOFT_END_MS),
    HYPNOTIC: Style(tempo=0.94, by_phrase=True, phrase_pause_ms=650, sentence_pause_ms=1500,
                    fade_in_ms=SOFT_START_MS, fade_out_ms=SOFT_END_MS),
}
# Script sections (see prompts/script_writer.md) -> style. EMERGENCE starts slower and ends natural.
SECTION_STYLES = {
    "PRE-TALK": NATURAL, "INDUCTION": HYPNOTIC, "DEEPENER": HYPNOTIC, "PART 1": SLOWER, "PART 2": SLOWER,
    "PART 3": SLOWER, "PART 4": HYPNOTIC, "EMERGENCE": "emergence",
}


def rate_for(style: str, session_rate: float) -> float:
    """The speed a style is spoken at, given the clinician's session speed."""
    tempo = STYLES[style].tempo
    return 1.0 if tempo is None else session_rate * tempo


def fades_for(style: str) -> tuple[int, int]:
    """The fades at the start and end of a piece of this style: (in, out) in ms."""
    return STYLES[style].fade_in_ms, STYLES[style].fade_out_ms


def _section_style(heading: str) -> str:
    title = heading.strip().upper()
    return next((style for name, style in SECTION_STYLES.items() if title.startswith(name)), SLOWER)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.findall(r"[^.!?]+[.!?]*", text) if s.strip()]


def _phrases(sentence: str) -> list[str]:
    """A sentence split after its commas, semicolons, colons and dashes; very short pieces join the next one."""
    pieces = [p.strip() for p in re.split(r"(?<=[,;:\u2014\u2013])\s+", sentence) if p.strip()]
    phrases: list[str] = []
    carry = ""
    for piece in pieces:
        text = f"{carry} {piece}".strip()
        if len(text.split()) < MIN_PHRASE_WORDS:
            carry = text
            continue
        phrases.append(text)
        carry = ""
    if carry:
        if phrases:
            phrases[-1] = f"{phrases[-1]} {carry}"
        else:
            phrases.append(carry)
    return phrases


def _paragraphs(script: str):
    """(section heading, paragraph text) for everything read aloud: no clinician notes, headings or '>' notes."""
    heading, skipping, lines = "", False, []
    for line in [*script.splitlines(), ""]:
        if line.startswith("## "):
            if lines:
                yield heading, " ".join(lines)
                lines = []
            heading = line[3:]
            skipping = heading.strip().upper().startswith("CLINICIAN NOTES")
            continue
        stripped = line.strip()
        if not stripped:
            if lines:
                yield heading, " ".join(lines)
                lines = []
            continue
        if skipping or stripped.startswith("#") or stripped.startswith(">"):
            continue
        lines.append(stripped)


def delivery_plan(script: str) -> list[dict]:
    """The session as the player speaks it: [{"text", "pause_ms", "style"}] in order. Every text is part of the
    script as the client receives it, so the voice will speak it."""
    items: list[dict] = []
    emergence: list[dict] = []
    for heading, paragraph in _paragraphs(script):
        section_style = _section_style(heading)
        pieces = re.split(r"(\(pause\)|\.\.\.|…)", spoken_form(paragraph), flags=re.IGNORECASE)
        for index in range(0, len(pieces), 2):
            marker = (pieces[index + 1] if index + 1 < len(pieces) else "").lower()
            segment = pieces[index].strip()
            if not segment:
                if marker and items:
                    items[-1]["pause_ms"] += LONG_PAUSE_MS if marker == "(pause)" else SHORT_PAUSE_MS
                continue
            for sentence in _sentences(segment):
                style = SLOWER if section_style == "emergence" else section_style
                spoken = _phrases(sentence) if STYLES[style].by_phrase else [sentence]
                for phrase in spoken:
                    item = {"text": phrase, "pause_ms": STYLES[style].phrase_pause_ms, "style": style}
                    items.append(item)
                    if section_style == "emergence":
                        emergence.append(item)
                items[-1]["pause_ms"] = STYLES[style].sentence_pause_ms
            if marker:
                items[-1]["pause_ms"] = max(items[-1]["pause_ms"],
                                            LONG_PAUSE_MS if marker == "(pause)" else SHORT_PAUSE_MS)
        if items:
            items[-1]["pause_ms"] += PARAGRAPH_PAUSE_MS
    for item in emergence[len(emergence) // 2:]:  # coming back up: the second half at a natural pace
        item["style"] = NATURAL
    if items:
        items[-1]["pause_ms"] = 0
    return items
