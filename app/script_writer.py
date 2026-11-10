"""Writes the personalised self-guided session script and checks it before it is delivered.

The script is written in two steps, so nothing is spent on the whole session before the client pays for it:
first the OPENING (title, pre-talk and induction), from which the free five-minute preview is made; then, once the
session is paid for, the REST (clinician notes, deepener, parts 1-4, emergence), written as a continuation of the
opening. assemble_script() joins them into one script with the sections in the usual order."""

import json
import re

from .knowledge import KnowledgeBase, render_documents, render_library_notes, sha
from .llm import ClaudeGateway, LLMResult

REQUIRED_SECTIONS = [
    "CLINICIAN NOTES", "PRE-TALK", "INDUCTION", "DEEPENER",
    "PART 1", "PART 2", "PART 3", "PART 4", "EMERGENCE",
]
OPENING_SECTIONS = ["PRE-TALK", "INDUCTION"]
MIN_OPENING_WORDS = 700  # the opening must fill the five-minute preview
SLEEP_CATEGORY = "sleep-and-insomnia"
# The recording lasts 60-85 minutes, about 5,500 spoken words (session_audio.py fits the pauses to that range).
MIN_SPOKEN_WORDS = 4000
MAX_SPOKEN_WORDS = 7000

TOUCH = "Contains a practitioner-touch step, which is impossible in a self-guided audio session"
LIGHTS = "Mentions flashing/flickering/strobe - check it is not light imagery (seizure-safety rule)"
HIDDEN = "Mentions subliminal content - nothing may be hidden from the client (ethics rule)"
FLAGGED_PATTERNS = [
    (r"\bI(?:'m going to| am going to| will|'ll) (?:touch|lift|press|tap|hold|move)\b", TOUCH),
    (r"\b(?:flash|flicker|strob)\w*", LIGHTS),
    (r"\bsubliminal\w*", HIDDEN),
]
# The same checks for a script in another language (the English ones still apply: a script may quote English).
FLAGGED_BY_LANGUAGE = {
    "de": [(r"\bich (?:werde|will) (?:Sie |Ihre? \w+ )?(?:berühren|anheben|drücken|antippen|halten|bewegen)\b",
            TOUCH),
           (r"\b\w*(?:blitz|flacker|flimmer|stroboskop)\w*", LIGHTS), (r"\bunterschwellig\w*", HIDDEN)],
    "fr": [(r"\bje (?:vais|veux) (?:vous )?(?:toucher|soulever|appuyer|tapoter|tenir|bouger)\b", TOUCH),
           (r"\b(?:clignot|scintill|stroboscop)\w*", LIGHTS), (r"\bsubliminal\w*", HIDDEN)],
    "es": [(r"\bvoy a (?:tocar|levantar|presionar|tocarle|sujetar|mover)\w*", TOUCH),
           (r"\b(?:parpade|destell|estrobosc)\w*", LIGHTS), (r"\bsubliminal\w*", HIDDEN)],
}
# What the pre-talk and the emergence must say, in each language: (opening the eyes, being fully alert) and never
# while driving; the emergence ends fully alert.
SAFETY_NET = {
    "en": (r"open your eyes", r"alert|awake"),
    "de": (r"Augen\s+(?:\w+\s+)?(?:zu\s+)?öffnen|öffnen Sie\s+(?:\w+\s+)?Augen|Augen\s+(?:\w+\s+)?auf",
           r"wach|aufmerksam|klar"),
    "fr": (r"ouvr\w*\s+(?:\w+\s+)?(?:les|vos)\s+yeux", r"éveill|alerte|réveill|lucide"),
    "es": (r"abr\w*\s+(?:\w+\s+)?(?:los|sus)\s+ojos", r"despiert|alerta|lúcid"),
}
DRIVING = {"en": r"\bdriv", "de": r"Auto|fahr|Steuer", "fr": r"condui|volant", "es": r"conduc|manejand|volante"}
FULLY_ALERT = {
    "en": r"(?:fully|wide) (?:alert|awake)|eyes open|open your eyes",
    "de": r"hellwach|(?:ganz|vollständig|völlig|wieder) wach|Augen\s+(?:\w+\s+)?(?:auf|öffnen|offen)|öffnen Sie",
    "fr": r"(?:pleinement|complètement|bien|tout à fait|parfaitement) (?:éveillé|réveillé|alerte)"
          r"|ouvr\w*\s+(?:\w+\s+)?(?:les|vos)\s+yeux|yeux ouverts",
    "es": r"(?:completamente|totalmente|bien|del todo) (?:despiert|alerta)"
          r"|abr\w*\s+(?:\w+\s+)?(?:los|sus)\s+ojos|ojos abiertos",
}
WORD = r"[^\W\d_]+(?:['’][^\W\d_]+)*"  # a spoken word, in any language


def spoken_words(text: str) -> int:
    return len(re.findall(WORD, spoken_text(text)))


def _headings(text: str) -> list[tuple[int, str]]:
    return [(i, line[3:].strip()) for i, line in enumerate(text.splitlines()) if line.startswith("## ")]


def section_text(text: str, name: str) -> str:
    lines = text.splitlines()
    headings = _headings(text)
    for position, (index, title) in enumerate(headings):
        if title.upper().startswith(name):
            end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
            return "\n".join(lines[index + 1:end])
    return ""


def spoken_text(text: str) -> str:
    """Everything that would be read aloud: drops clinician notes, headings and '>' delivery notes."""
    kept = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = line[3:].strip().upper().startswith("CLINICIAN NOTES")
            continue
        stripped = line.lstrip()
        if skipping or stripped.startswith("#") or stripped.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def patient_version(text: str) -> str:
    title = next((line for line in text.splitlines() if line.startswith("# ")), "")
    body = re.sub(r"\n{3,}", "\n\n", spoken_text(text)).strip()
    return f"{title}\n\n{body}".strip() if title else body


def _pre_talk_issues(text: str, language: str = "en") -> list[str]:
    issues = []
    pre_talk = section_text(text, "PRE-TALK")
    eyes, alert = SAFETY_NET.get(language, SAFETY_NET["en"])
    if not (re.search(eyes, pre_talk, re.I) and re.search(alert, pre_talk, re.I)):
        issues.append("Pre-talk is missing the safety-net line (open your eyes and be fully alert at any time)")
    if not re.search(DRIVING.get(language, DRIVING["en"]), pre_talk, re.I):
        issues.append("Pre-talk is missing the safe-listening reminder (never while driving)")
    return issues


def _flagged(text: str, language: str = "en") -> list[str]:
    spoken = spoken_text(text)
    patterns = FLAGGED_PATTERNS + FLAGGED_BY_LANGUAGE.get(language, [])
    return list(dict.fromkeys(label for pattern, label in patterns if re.search(pattern, spoken, re.I)))


def validate_opening(text: str, *, stop_reason: str | None, language: str = "en") -> list[str]:
    """The opening alone: a title, then exactly PRE-TALK and INDUCTION, with the pre-talk's safety lines."""
    issues = []
    if stop_reason == "max_tokens":
        issues.append("The opening was cut off before the end (output limit reached)")
    if not any(line.startswith("# ") for line in text.splitlines()):
        issues.append("Missing the title line (# ...)")
    titles = [title.upper() for _, title in _headings(text)]
    found = [next((name for name in OPENING_SECTIONS if title.startswith(name)), None) for title in titles]
    if found != OPENING_SECTIONS:
        issues.append("The opening must contain exactly the sections ## PRE-TALK and then ## INDUCTION")
    issues += _pre_talk_issues(text, language)
    issues += _flagged(text, language)
    words = spoken_words(text)
    if words < MIN_OPENING_WORDS:
        issues.append(f"The opening is too short: {words} spoken words (expected about 1,200)")
    return issues


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading line, body) for every ## section, in order."""
    lines = text.splitlines()
    headings = _headings(text)
    return [(lines[index], "\n".join(lines[index + 1:(headings[n + 1][0] if n + 1 < len(headings) else len(lines))]))
            for n, (index, _) in enumerate(headings)]


def assemble_script(opening: str, rest: str) -> str:
    """The complete script: the opening's title, the rest's CLINICIAN NOTES, the opening's PRE-TALK and INDUCTION
    exactly as recorded for the preview, then the rest from DEEPENER on. Anything the rest repeats of the opening
    (a title, a pre-talk or an induction) is ignored."""
    title = next((line for line in opening.splitlines() if line.startswith("# ")), "")
    parts = [title] if title else []
    parts += [f"{heading}\n{body.strip()}" for heading, body in _sections(rest)
              if heading[3:].strip().upper().startswith("CLINICIAN NOTES")]
    parts += [f"{heading}\n{body.strip()}" for heading, body in _sections(opening)]
    parts += [f"{heading}\n{body.strip()}" for heading, body in _sections(rest)
              if not heading[3:].strip().upper().startswith(("CLINICIAN NOTES", *OPENING_SECTIONS))]
    return "\n\n".join(parts)


def validate_script(text: str, *, category: str, stop_reason: str | None, language: str = "en") -> list[str]:
    issues = []
    if stop_reason == "max_tokens":
        issues.append("The script was cut off before the end (output limit reached)")

    titles = [title.upper() for _, title in _headings(text)]
    positions = []
    for name in REQUIRED_SECTIONS:
        found = next((i for i, title in enumerate(titles) if title.startswith(name)), None)
        if found is None:
            issues.append(f"Missing section: {name}")
        else:
            positions.append(found)
    if positions != sorted(positions):
        issues.append("Sections are not in the required order")

    issues += _pre_talk_issues(text, language)
    if category != SLEEP_CATEGORY:
        emergence = section_text(text, "EMERGENCE")
        if not re.search(FULLY_ALERT.get(language, FULLY_ALERT["en"]), emergence, re.I):
            issues.append("Emergence does not clearly end in full alertness")

    issues += _flagged(text, language)

    words = spoken_words(text)
    if words < MIN_SPOKEN_WORDS:
        issues.append(f"Script is short: {words} spoken words (expected about 5,500 for a 60-85 minute session)")
    elif words > MAX_SPOKEN_WORDS:
        issues.append(f"Script is long: {words} spoken words (expected about 5,500 for a 60-85 minute session)")
    return issues


def retrieval_query(intake: dict) -> str:
    parts = [
        intake.get("presenting_issue", ""),
        intake.get("presenting_issue_category", "").replace("-", " "),
        intake.get("goal_in_client_words", ""),
        " ".join(intake.get("knowledge_search_terms", [])),
        " ".join(t.get("situation", "") for t in intake.get("current_triggers", [])),
        " ".join(f"{m.get('feeling', '')} {m.get('belief_formed', '')}" for m in intake.get("origin_moments", [])),
    ]
    return " ".join(p for p in parts if p)


def render_lessons(lessons: list[dict]) -> str:
    if not lessons:
        return "<clinician_lessons>\n(none yet)\n</clinician_lessons>"
    body = "\n".join(f"- {lesson['lesson']}" for lesson in lessons)
    return f"<clinician_lessons>\n{body}\n</clinician_lessons>"


def render_screening(version: str, questions: list[dict], answers: dict) -> str:
    lines = [f"- {q['text']} {'Yes' if answers[q['id']] else 'No'}" for q in questions]
    return f'<screening version="{version}" outcome="clear">\n' + "\n".join(lines) + "\n</screening>"


def must_fix(issues: list[str]) -> list[str]:
    """The problems a script may never be delivered with: everything except its length being a little off."""
    return [issue for issue in issues if not issue.startswith(("Script is short", "Script is long"))]


OPENING_REQUEST = (
    "Write only the OPENING of this client's session now: the title line (# ...), then ## PRE-TALK and ## INDUCTION "
    "in full, exactly as they will appear in the complete script (about 1,200 spoken words together). Stop at the "
    "end of the induction, at a natural point where the deepener can follow. Do not write CLINICIAN NOTES or "
    "anything after the induction: the rest of the session is written later, as a continuation of your opening. "
    "The first five minutes of the opening become the client's free preview of their session."
)
REST_REQUEST = (
    "The opening of this client's session is below. It has already been recorded for them, so it cannot change.\n\n"
    "<session_opening>\n{opening}\n</session_opening>\n\n"
    "Now write the rest of the session as a seamless continuation of that opening: first ## CLINICIAN NOTES "
    "(covering the whole session, the opening included), then ## DEEPENER, the four ## PART sections and "
    "## EMERGENCE, with the headings and lengths the instructions give. Carry on from exactly where the induction "
    "ends. Do not repeat or rewrite the title, the pre-talk or the induction."
)


async def write_script(gateway: ClaudeGateway, kb: KnowledgeBase, *, system_prompt: str, intake: dict,
                       lessons: list[dict], screening: str, effort: str, fix: list[str] | None = None,
                       part: str = "whole", opening: str | None = None) -> tuple[str, list[dict], LLMResult]:
    """The session script, or one part of it: "opening" (title, pre-talk, induction) or "rest" (continuing from
    `opening`). `fix`: what the automatic checks found wrong with the previous draft, to be put right."""
    category = intake["presenting_issue_category"]
    reference = kb.script_reference(category)
    notes = kb.search(retrieval_query(intake))

    system = [
        {
            "type": "text",
            "text": f"{system_prompt}\n\n{render_documents(reference, 'practice_reference')}",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": render_lessons(lessons)},
    ]
    user = (
        f"{render_library_notes(notes)}\n\n"
        f"{screening}\n\n"
        f"<client_intake>\n{json.dumps(intake, indent=2, ensure_ascii=False)}\n</client_intake>\n\n"
    )
    if part == "opening":
        user += OPENING_REQUEST
    elif part == "rest":
        user += REST_REQUEST.format(opening=opening)
    else:
        user += "Write the complete self-guided session script now."
    if fix:
        user += ("\n\nA previous draft failed these automatic checks. Write it again and make sure every one of "
                 "them is met:\n" + "\n".join(f"- {issue}" for issue in fix))
    result = await gateway.complete(
        system=system, messages=[{"role": "user", "content": user}], effort=effort, max_tokens=64000,
    )
    sources = [{"kind": "reference", "path": path, "sha": sha(text)} for path, text in reference] + [
        {"kind": "library", "path": c.source_path, "section": c.heading, "sha": c.sha} for c in notes
    ]
    return result.text.strip(), sources, result
