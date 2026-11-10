"""Test doubles: a scripted stand-in for the Claude API and a small synthetic knowledge base."""

from pathlib import Path

from app.consultation import AnswerAnalysis, ConsultantTurn
from app.knowledge import PRESENTING_ISSUES
from app.llm import LLMResult
from app.risk import RiskAssessment
from app.script_writer import OPENING_REQUEST

FAKE_USAGE = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0}

EXCLUDED_MARKERS = ("BAD_MARKER", "MISLABELED_MARKER", "CAUTION_BOOK_MARKER", "UNLISTED_MARKER",
                    "MALFORMED_MARKER", "FLAGGED_MARKER", "FLAGGED_SUB_MARKER", "SKIPPED_MARKER")


def turn(reply: str, complete: bool = False, note: str = "", facts: list[str] | None = None) -> ConsultantTurn:
    analysis = AnswerAnalysis(facts=facts or ["a stated fact"], feelings=[], beliefs_or_conclusions=[],
                              links_to_earlier_answers=[], inconsistencies=[], gaps=[], next_focus="the next thread")
    return ConsultantTurn(analysis=analysis, delivery="gentle", reply=reply, consultation_complete=complete,
                          coverage_note=note)


def split_script(script: str) -> tuple[str, str]:
    """A complete script as the writer produces it in two steps: (opening: title, pre-talk, induction;
    rest: clinician notes, deepener ... emergence)."""
    blocks = script.split("\n## ")
    title, sections = blocks[0], ["## " + block for block in blocks[1:]]
    opening = [b for b in sections if b[3:].upper().startswith(("PRE-TALK", "INDUCTION"))]
    rest = [b for b in sections if b not in opening]
    return "\n\n".join([title.strip(), *[b.strip() for b in opening]]), "\n\n".join(b.strip() for b in rest)


class FakeGateway:
    """Each call takes the next queued response for its kind. Queue an Exception to make that call fail.
    A queued complete script answers a request for its opening or its rest with that part only."""

    def __init__(self):
        self.model = "claude-opus-5"
        self.parsed: dict[str, list] = {}
        self.completions: list = []
        self.calls: list[dict] = []

    def queue_parse(self, model_name: str, item) -> None:
        self.parsed.setdefault(model_name, []).append(item)

    def _result(self, text="", stop_reason="end_turn") -> LLMResult:
        return LLMResult(text, stop_reason, self.model, dict(FAKE_USAGE), "req_test")

    async def parse(self, *, system, messages, output_model, effort, max_tokens):
        self.calls.append({"kind": "parse", "model": output_model.__name__, "system": system, "messages": messages})
        queue = self.parsed.get(output_model.__name__, [])
        if not queue and output_model is RiskAssessment:
            return RiskAssessment(level="none", categories=[], evidence="", rationale="ok"), self._result()
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, self._result()

    async def complete(self, *, system, messages, effort, max_tokens):
        request = messages[-1]["content"]
        part = "opening" if OPENING_REQUEST in request else "rest" if "<session_opening>" in request else None
        self.calls.append({"kind": "complete", "system": system, "messages": messages, "part": part})
        item = self.completions.pop(0)
        if isinstance(item, Exception):
            raise item
        text, stop_reason = item
        if part and "## CLINICIAN NOTES" in text and "## PRE-TALK" in text:
            text = split_script(text)[0 if part == "opening" else 1]
        return self._result(text, stop_reason)


class FakeVoice:
    """Stands in for the practice voice (Pocket TTS): each text is 'spoken' as a few PCM bytes."""

    AUDIO = (b"\x01\x00" * 4, b"\x02\x00" * 4)

    def __init__(self, available: bool = True, error: Exception | None = None, languages=("en",)):
        self.available = available
        self.languages = set(languages)  # the languages its voice is prepared for
        self.error = error
        self.failed = None
        self.failed_languages = {}
        self.state_path = Path("practice_voice.safetensors")
        self.warmed: list[tuple[str, float]] = []
        self.spoken: list[str] = []
        self.rates: list[float] = []
        self.softened: list = []
        self.rendered: list[tuple[str, float, tuple | None]] = []

    def speaks(self, language: str) -> bool:
        return self.available and language in self.languages

    def start(self, warm_up=(), rate: float = 1.0) -> None:
        for language, texts in (warm_up if isinstance(warm_up, dict) else {"en": warm_up}).items():
            if self.speaks(language):
                self.warm(texts, rate, language)

    def warm(self, texts, rate: float, language: str = "en") -> None:
        self.warmed += [(text, rate) for text in texts]

    def close(self) -> None:
        pass

    def render(self, text: str, rate: float = 1.0, soften_ms=None, language: str = "en") -> bytes:
        """A whole session piece at once (for the session's audio file): 10 ms of PCM."""
        if self.error:
            raise self.error
        self.rendered.append((text, rate, soften_ms))
        return b"\x01\x00" * 240

    async def stream(self, text: str, rate: float = 1.0, soften_ms=None, language: str = "en"):
        if self.error:
            raise self.error
        self.spoken.append(text)
        self.rates.append(rate)
        self.softened.append(soften_ms)
        for chunk in self.AUDIO:
            yield chunk


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_knowledge_base(root: Path) -> Path:
    write(root, "knowledge_base/index.md",
          "# Index\n\n## Ethical frame (read this first, every time)\n\nETHICS_MARKER no covert technique.\n\n"
          "## How the agent uses this folder\n\nNOT_ETHICS_MARKER\n")
    write(root, "knowledge_base/book_catalog.md", "\n".join([
        "# Catalog", "",
        "| File | Tag | Status |",
        "|---|---|---|",
        "| Good Book.pdf | core-craft | ingested: see `ingested/good-book.md` |",
        "| Bad Book.pdf | excluded | n/a see `ingested/bad-book.md` |",
        "| Mislabeled.pdf | **mislabeled tag, now excluded**, covert | reviewed, see `ingested/mislabeled.md` |",
        "| Caution Book.pdf | caution | ingested, see `ingested/caution-book.md` |",
        "| Broken | row | with | too many cells `ingested/malformed-ref.md` |",
        "",
    ]))
    write(root, "knowledge_base/ingested/malformed-ref.md", "# Malformed\n\nMALFORMED_MARKER smoking\n")
    write(root, "knowledge_base/ingested/good-book.md",
          "# Good Book\n\nIntro text about the book.\n\n"
          "## Smoking trigger technique\n\nSMOKING_MARKER anchor for smoking triggers at work coffee break.\n\n"
          "## Techniques flagged and excluded\n\nFLAGGED_MARKER covert smoking thing.\n\n"
          "### A specific excluded item\n\nFLAGGED_SUB_MARKER smoking.\n\n"
          "## Skipped / Not Used\n\nSKIPPED_MARKER smoking\n\n"
          "## Diamond reframe: cautionary-tale metaphor\n\nCAUTIONARY_OK_MARKER smoking reframe.\n")
    write(root, "knowledge_base/ingested/bad-book.md", "# Bad\n\nBAD_MARKER smoking smoking smoking\n")
    write(root, "knowledge_base/ingested/mislabeled.md", "# Mis\n\nMISLABELED_MARKER smoking\n")
    write(root, "knowledge_base/ingested/caution-book.md", "# Caution\n\nCAUTION_BOOK_MARKER smoking\n")
    write(root, "knowledge_base/ingested/unlisted.md", "# Unlisted\n\nUNLISTED_MARKER smoking\n")
    for name in ("screening-and-safety", "emotional-pivot-reprogramming", "suggestion-language",
                 "ego-strengthening-and-emergence", "inductions", "deepeners"):
        write(root, f"knowledge_base/techniques/{name}.md", f"# {name}\n\n{name.upper()}_MARKER\n")
    for issue in PRESENTING_ISSUES:
        write(root, f"knowledge_base/techniques/presenting-issues/{issue}.md", f"# {issue}\n\nISSUE_{issue}_MARKER\n")
    write(root, "templates/script_structure.md", "# Skeleton\n\nTEMPLATE_MARKER\n")
    return root


def valid_script(words_per_section: int = 700, name: str = "Sam") -> str:
    filler = " ".join(["breathing slowly and comfortably"] * (words_per_section // 4))
    return "\n\n".join([
        f"# Smoking Self-Guided Hypnotherapy Session for {name}",
        "## CLINICIAN NOTES\nUsed the coffee-break origin moment. Not read aloud.",
        "## PRE-TALK\nListen only when you can relax safely, never while driving. At any time, if you need to, "
        f"you can open your eyes and be fully alert immediately. {filler}",
        f"## INDUCTION\n{filler}",
        f"## DEEPENER\n{filler}",
        f"## PART 1 - The Lantern Workshop: Belief Archaeology & Transformation\n{filler}\n> slow down here",
        f"## PART 2 - The Quiet Harbour: Parts Dialogue & Integration\n{filler}",
        f"## PART 3 - Future Pacing\n{filler}",
        f"## PART 4 - Post-Hypnotic Anchors\n{filler}",
        f"## EMERGENCE\n{filler} Five... eyes open, fully alert and refreshed.",
    ])
