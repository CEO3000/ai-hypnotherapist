"""Books the practice adds in the admin panel (as PDF files), turned into technique notes the AI Hypnotherapist
uses when it writes sessions. No coding needed: everything happens in the dashboard's Books tab.

1. Upload: the server reads the PDF's text (pypdf, BSD licence) and then deletes the file. A PDF of scanned pages
   has no text to read and is refused with a clear message.
2. "Add to the AI": the AI first checks what the book is. Books of covert influence, "dark psychology", manipulation
   or stage hypnosis are refused, as they are for the practice's built-in library.
3. The AI reads the book part by part and writes technique notes in its own words (never copying passages),
   leaving out anything covert, touch-based or outside the practice's ethics. Its progress is shown; if the app
   stops half way, "Try again" carries on from the last finished part.
4. The practice reads the notes and approves them (or discards them). Only approved notes are used, as supporting
   craft for the sessions the AI writes. A book can be removed again at any time.
"""

import asyncio
import hashlib
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pypdf import PdfReader

from .llm import LLMRefusal, LLMUnavailable, LLMUsageLimit
from .text_rules import no_em_dashes

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 150 * 1024 * 1024
MAX_PAGES = 2000
MAX_CHARS = 4_000_000  # about 650,000 words
MIN_CHARS_PER_PAGE = 100  # fewer: the pages are scanned pictures, with no text to read
PART_CHARS = 40_000  # one AI request reads about 6,500 words of the book
SAMPLE_CHARS = (14_000, 6_000, 6_000)  # the beginning, middle and end the AI checks the book from
NO_NOTES = "NO_NOTES"
SUITABLE = ("core-craft", "supplementary")
WORKING = ("reading", "checking", "writing")  # the app is busy with the book
# Rough sizes for the cost estimate: the instructions sent with each part, and the notes written for it.
PROMPT_TOKENS_PER_PART, NOTES_TOKENS_PER_PART, CHECK_TOKENS = 1_500, 2_500, 8_000


class Conflict(Exception):
    """The request does not fit the book's current state (the message is shown to the practice)."""


class BookCheck(BaseModel):
    title: str = Field(description="The book's title as printed in it, or the file name")
    author: str = Field(description="The author(s) as printed in the book, or empty")
    category: Literal["core-craft", "supplementary", "caution", "excluded", "not_relevant"]
    topics: list[str] = Field(description="A few short topics, for example 'smoking', 'Ericksonian language'")
    reason: str = Field(description="One or two plain sentences for the practice explaining the decision")


def read_pdf(path: Path) -> tuple[int, str]:
    """(pages, text) of a PDF; raises ValueError with a message for the practice when it can't be used."""
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("This PDF is password-protected. Please upload a copy without a password.")
        pages = len(reader.pages)
        if pages > MAX_PAGES:
            raise ValueError(f"This PDF has {pages} pages; books of up to {MAX_PAGES} pages can be added.")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - any damaged or unusual file gets the same plain answer
        raise ValueError("This file could not be read as a PDF.") from exc
    text = re.sub(r"[ \t ]+", " ", text.replace("\x00", ""))
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text).strip()
    if len(text) < max(2_000, MIN_CHARS_PER_PAGE * pages // 2):
        raise ValueError("This PDF has almost no text that can be read: its pages are probably scanned pictures. "
                         "Please use a PDF where the text can be selected.")
    if len(text) > MAX_CHARS:
        raise ValueError("This book is too long to add in one go. Please split it into smaller PDFs.")
    return pages, text


def split_parts(text: str, size: int | None = None) -> list[str]:
    """The book in parts of about `size` (PART_CHARS) characters, cut between paragraphs where possible."""
    size = size or PART_CHARS
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > size:
            cut = paragraph.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if current:
                parts.append(current)
                current = ""
            parts.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if current and len(current) + 2 + len(paragraph) > size:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        parts.append(current)
    return parts


def sample(text: str) -> str:
    """The beginning (contents and introduction), middle and end of the book, for the suitability check."""
    start, middle, end = SAMPLE_CHARS
    if len(text) <= start + middle + end:
        return text
    mid = len(text) // 2
    late = int(len(text) * 0.8)
    return "\n\n[...]\n\n".join([text[:start], text[mid:mid + middle], text[late:late + end]])


def clean_notes(text: str) -> str:
    """One part's notes as ## sections only (NO_NOTES: nothing), with no em dashes."""
    text = text.strip()
    if not text or text.startswith(NO_NOTES):
        return ""
    text = re.sub(r"(?m)^#(?=\s)", "##", text)  # a stray top-level heading becomes a section
    start = text.find("## ")
    return no_em_dashes(text[start:] if start > 0 else text).strip()


def estimate(chars: int, parts: int, prices: tuple[float, float] | None) -> dict:
    """What adding the book takes: AI requests, tokens and (with the pay-as-you-go API) the cost in US dollars."""
    tokens_in = chars // 4 + parts * PROMPT_TOKENS_PER_PART + CHECK_TOKENS
    tokens_out = parts * NOTES_TOKENS_PER_PART + 500
    cost = None if prices is None else round((tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000, 2)
    return {"requests": parts + 1, "input_tokens": tokens_in, "output_tokens": tokens_out, "usd": cost}


class Library:
    def __init__(self, *, db, gateway, data_dir: Path, prompts: dict, record, prices: tuple[float, float] | None,
                 effort: str = "medium"):
        self.db = db
        self.gateway = gateway
        self.prompts = prompts  # {"book_check": ..., "book_notes": ...}
        self.record = record  # (purpose, LLMResult) -> None: usage accounting
        self.prices = prices
        self.effort = effort
        root = data_dir / "library"
        self.approved_dir = root / "books"  # approved notes, one file per book: read by the knowledge store
        self.text_dir = root / "text"  # a book's text while it is being added
        self.upload_dir = root / "uploads"  # an uploaded PDF, only until its text is read
        self._lock = asyncio.Lock()  # one book at a time

    def startup(self) -> None:
        for folder in (self.approved_dir, self.text_dir, self.upload_dir):
            folder.mkdir(parents=True, exist_ok=True)
        for book in self.db.list_books():
            if book["status"] in WORKING:
                self.db.move_book(book["id"], (book["status"],), "failed",
                                  error="The app was restarted while it was working on this book. Press Try again.")

    # --- files ---------------------------------------------------------------------------------------------

    def _text_path(self, book_id: int) -> Path:
        return self.text_dir / f"{book_id}.txt"

    def _upload_path(self, book_id: int) -> Path:
        return self.upload_dir / f"{book_id}.pdf"

    def notes_path(self, book_id: int) -> Path:
        return self.approved_dir / f"book-{book_id}.md"

    def _text(self, book_id: int) -> str:
        return self._text_path(book_id).read_text(encoding="utf-8")

    # --- what the dashboard shows ----------------------------------------------------------------------------

    def view(self, book: dict, with_notes: bool = False) -> dict:
        shown = {key: book[key] for key in ("id", "filename", "status", "pages", "words", "parts", "parts_done",
                                            "title", "author", "category", "topics", "reason", "error",
                                            "created_at", "approved_at")}
        notes = book.get("notes") or ""
        shown["sections"] = len(re.findall(r"(?m)^## ", notes))
        text = self._text_path(book["id"])
        if book["status"] in ("read", "failed") and book.get("parts") and text.exists():
            shown["estimate"] = estimate(text.stat().st_size, book["parts"], self.prices)
        if with_notes:
            shown["notes"] = notes
        return shown

    def list(self) -> list[dict]:
        return [self.view(book) for book in self.db.list_books()]

    def get(self, book_id: int, with_notes: bool = False) -> dict | None:
        book = self.db.get_book(book_id)
        return self.view(book, with_notes) if book else None

    # --- steps -------------------------------------------------------------------------------------------------

    def add(self, filename: str, pdf: Path) -> int:
        """A new upload (the file at `pdf` is moved into the library); its text is read next (read())."""
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if self.db.book_with_hash(digest):
            pdf.unlink(missing_ok=True)
            raise Conflict("This book has already been added.")
        book_id = self.db.add_book(filename=filename, file_hash=digest)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdf), self._upload_path(book_id))
        return book_id

    async def read(self, book_id: int) -> None:
        pdf = self._upload_path(book_id)
        try:
            pages, text = await asyncio.to_thread(read_pdf, pdf)
        except ValueError as exc:
            self.db.move_book(book_id, ("reading",), "unreadable", error=str(exc))
            return
        finally:
            pdf.unlink(missing_ok=True)  # the PDF itself is never kept
        self.text_dir.mkdir(parents=True, exist_ok=True)
        self._text_path(book_id).write_text(text, encoding="utf-8")
        self.db.move_book(book_id, ("reading",), "read", pages=pages, words=len(text.split()),
                          parts=len(split_parts(text)))

    def start(self, book_id: int) -> None:
        """"Add to the AI" (or "Try again"): checked first, then its notes are written, carrying on from the last
        finished part."""
        book = self.db.get_book(book_id)
        if book is None or book["status"] not in ("read", "failed"):
            raise Conflict("This book can't be added at the moment.")
        if not self._text_path(book_id).exists():
            raise Conflict("The book's text is no longer here. Please remove it and upload the PDF again.")
        step = "writing" if book["category"] in SUITABLE else "checking"
        if not self.db.move_book(book_id, ("read", "failed"), step, error=None):
            raise Conflict("This book changed; please reload.")

    async def process(self, book_id: int) -> None:
        async with self._lock:
            try:
                await self._check(book_id)
                await self._write_notes(book_id)
            except (LLMUnavailable, LLMRefusal) as exc:
                log.error("Book %s: the AI could not finish: %s", book_id, exc)
                message = ("The AI has reached its usage limit for now. Press Try again later: it carries on "
                           "from where it stopped.") if isinstance(exc, LLMUsageLimit) else (
                           "The AI could not finish this book just now. Press Try again: it carries on from where "
                           "it stopped.")
                book = self.db.get_book(book_id)
                if book and book["status"] in WORKING:
                    self.db.move_book(book_id, (book["status"],), "failed", error=message)

    async def _check(self, book_id: int) -> None:
        book = self.db.get_book(book_id)
        if book is None or book["status"] != "checking":
            return
        text = self._text(book_id)
        request = (f"<book file_name=\"{book['filename']}\" pages=\"{book['pages']}\" words=\"{book['words']}\">\n"
                   f"{sample(text)}\n</book>\n\nCheck this book.")
        check, result = await self.gateway.parse(
            system=[{"type": "text", "text": self.prompts["book_check"]}],
            messages=[{"role": "user", "content": request}], output_model=BookCheck, effort=self.effort,
            max_tokens=4000)
        self.record("book_check", result)
        fields = {"title": check.title.strip() or book["filename"], "author": check.author.strip(),
                  "category": check.category, "topics": check.topics[:8], "reason": no_em_dashes(check.reason)}
        if check.category not in SUITABLE:
            self._text_path(book_id).unlink(missing_ok=True)
            self.db.move_book(book_id, ("checking",), "refused", **fields)
            return
        self.db.move_book(book_id, ("checking",), "writing", notes="", parts_done=0, **fields)

    async def _write_notes(self, book_id: int) -> None:
        book = self.db.get_book(book_id)
        if book is None or book["status"] != "writing":
            return
        parts = split_parts(self._text(book_id))
        notes = book["notes"] or ""
        by = f" by {book['author']}" if book["author"] else ""
        for index in range(book["parts_done"], len(parts)):
            request = (f"<book title=\"{book['title']}\" author=\"{book['author']}\" part=\"{index + 1} of "
                       f"{len(parts)}\">\n{parts[index]}\n</book>\n\nWrite the technique notes for this part of "
                       f"{book['title']}{by}.")
            result = await self.gateway.complete(
                system=[{"type": "text", "text": self.prompts["book_notes"]}],
                messages=[{"role": "user", "content": request}], effort=self.effort, max_tokens=16000)
            self.record("book_notes", result)
            written = clean_notes(result.text)
            if written:
                notes = f"{notes}\n\n{written}".strip()
            self.db.set_book_fields(book_id, notes=notes, parts_done=index + 1)
        self.db.move_book(book_id, ("writing",), "review", parts=len(parts))

    def approve(self, book_id: int) -> None:
        """The practice has read the notes: from now on the AI uses them."""
        book = self.db.get_book(book_id)
        if book is None or book["status"] != "review":
            raise Conflict("Only a book whose notes are ready can be approved.")
        if not (book["notes"] or "").strip():
            raise Conflict("The AI found nothing in this book to use for sessions, so there is nothing to approve.")
        topics = ", ".join(book["topics"] or [])
        added = datetime.now(timezone.utc).strftime("%d %B %Y")
        header = (f"# {book['title']}" + (f" ({book['author']})" if book["author"] else "") + "\n\n"
                  f"Added by the practice on {added}. Kind of book: {book['category']}."
                  + (f" Topics: {topics}." if topics else "") + "\n\n")
        self.approved_dir.mkdir(parents=True, exist_ok=True)
        self.notes_path(book_id).write_text(header + book["notes"].strip() + "\n", encoding="utf-8")
        if not self.db.move_book(book_id, ("review",), "approved",
                                 approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds")):
            self.notes_path(book_id).unlink(missing_ok=True)
            raise Conflict("This book changed; please reload.")
        self._text_path(book_id).unlink(missing_ok=True)

    def remove(self, book_id: int) -> None:
        """Takes the book out completely (the AI stops using it at once)."""
        book = self.db.get_book(book_id)
        if book is None:
            raise Conflict("This book is no longer here.")
        if book["status"] in WORKING:
            raise Conflict("The app is working on this book. Please wait until it has finished.")
        for path in (self.notes_path(book_id), self._text_path(book_id), self._upload_path(book_id)):
            path.unlink(missing_ok=True)
        self.db.delete_book(book_id)
