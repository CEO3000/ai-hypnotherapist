"""The client's consultation as a PDF: every question the AI Hypnotherapist asked and every answer the client gave.
It goes to the practice with a client's request for a qualified hypnotherapist (see notify.human_request_email).
Made with ReportLab (BSD licence) and the Bitstream Vera fonts that come with it (free to use and share)."""

import unicodedata
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from .notify import UK_TIME

FONT_DIR = Path(reportlab.__file__).parent / "fonts"
FONT, BOLD = "Vera", "VeraBd"
_fonts: dict[str, TTFont] = {}


def _font() -> TTFont:
    if not _fonts:
        for name, file in ((FONT, "Vera.ttf"), (BOLD, "VeraBd.ttf")):
            _fonts[name] = TTFont(name, str(FONT_DIR / file))
            pdfmetrics.registerFont(_fonts[name])
    return _fonts[FONT]


def _plain(text: str) -> str:
    """The text with any character the font cannot show replaced by its plain form (a letter the font lacks loses
    its accent, an emoji becomes "?"), so nothing is silently lost from the page."""
    glyphs = _font().face.charToGlyph
    return "".join(c if c == "\n" or ord(c) in glyphs
                   else unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode() or "?" for c in text)


def _printable(text: str) -> str:
    """_plain, made safe inside a Paragraph (markup characters escaped, line breaks kept)."""
    return escape(_plain(text)).replace("\n", "<br/>")


def _uk(stamp: str | None) -> str:
    if not stamp:
        return "-"
    return datetime.fromisoformat(stamp).astimezone(UK_TIME).strftime("%d %B %Y, %H:%M")


def consultation_pdf(*, practice: str, client: dict, session_ref: str, started_at: str | None,
                     messages: list[dict]) -> bytes:
    """client: {"name", "email", "phone"}; messages: [{"role": "assistant"|"patient", "content", "created_at"}]."""
    _font()
    base = ParagraphStyle("base", fontName=FONT, fontSize=10.5, leading=15, textColor=HexColor("#222222"))
    title = ParagraphStyle("title", parent=base, fontName=BOLD, fontSize=17, leading=22, spaceAfter=4)
    small = ParagraphStyle("small", parent=base, fontSize=9, leading=13, textColor=HexColor("#555555"))
    label = ParagraphStyle("label", parent=base, fontName=BOLD, fontSize=9.5, spaceBefore=9,
                           textColor=HexColor("#1f5f4a"))
    answer_label = ParagraphStyle("answer_label", parent=label, spaceBefore=3, textColor=HexColor("#444444"))
    answer = ParagraphStyle("answer", parent=base, leftIndent=5 * mm)

    story = [
        Paragraph(_printable("Consultation: questions and answers"), title),
        Paragraph(_printable(f"{practice}, AI Hypnotherapist consultation"), small),
        Spacer(1, 4 * mm),
        Paragraph(_printable(f"Client: {client['name']}"), base),
        Paragraph(_printable(f"Email: {client['email']}"), base),
        Paragraph(_printable(f"Mobile: {client['phone']}"), base),
        Paragraph(_printable(f"Consultation started: {_uk(started_at)} (UK time)"), base),
        Paragraph(_printable(f"Session reference: {session_ref}"), base),
        Spacer(1, 3 * mm),
        Paragraph(_printable("Every question below was asked by the AI Hypnotherapist; each answer is the client's "
                             "own, as they gave it (spoken answers were written down by their browser)."), small),
    ]
    number = 0
    for index, message in enumerate(messages):
        text = message["content"].strip()
        if message["role"] == "assistant":
            answered = index + 1 < len(messages) and messages[index + 1]["role"] == "patient"
            if answered:
                number += 1
            heading = f"Question {number}" if answered else "AI Hypnotherapist"
            block = [Paragraph(_printable(heading), label), Paragraph(_printable(text), base)]
        else:
            heading = f"Answer {number}" if number else "Client"
            block = [Paragraph(_printable(heading), answer_label), Paragraph(_printable(text), answer)]
        story.append(KeepTogether(block))

    buffer = BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 8)
        canvas.setFillColor(HexColor("#777777"))
        canvas.drawString(18 * mm, 10 * mm, _plain(f"Confidential:{client['name']}, {session_ref}"))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                      bottomMargin=18 * mm, title="Consultation: questions and answers", author=practice,
                      ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
