"""Books the practice adds in the admin panel: read, checked, turned into notes, approved, used, removed."""

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.books import BookCheck, clean_notes, estimate, split_parts
from app.llm import LLMUnavailable, LLMUsageLimit
from tests.conftest import ADMIN_HEADER, CLINICIAN_AUTH

EM_DASH = "—"
PARAGRAPH = ("The therapist invites the client to notice the breath and to let each breath out carry a little more "
             "tension away, pacing what is already happening before leading gently towards calm. ")


def make_pdf(pages: int = 3, text: str = PARAGRAPH * 9) -> bytes:
    """A small PDF with real, selectable text on every page (pages=0 text: a 'scanned' PDF with none)."""
    out = BytesIO()
    page = canvas.Canvas(out, pagesize=A4)
    for number in range(pages):
        if text:
            lines = [text[i:i + 95] for i in range(0, len(text), 95)]
            y = 800
            for line in [f"Chapter {number + 1}", *lines]:
                page.drawString(40, y, line)
                y -= 14
        else:
            page.rect(40, 400, 300, 300, fill=1)  # a picture, no text
        page.showPage()
    page.save()
    return out.getvalue()


def upload(client, data: bytes, name: str = "Trance Craft.pdf", auth=CLINICIAN_AUTH):
    return client.post(f"/api/clinician/books?name={name}", content=data, auth=auth,
                       headers={"Content-Type": "application/pdf", **ADMIN_HEADER})


def books(client) -> list[dict]:
    return client.get("/api/clinician/books", auth=CLINICIAN_AUTH).json()["books"]


def suitable(**overrides) -> BookCheck:
    return BookCheck(**{"title": "Trance Craft", "author": "A. Author", "category": "core-craft",
                        "topics": ["breathing", "pacing and leading"],
                        "reason": "A clinical hypnotherapy manual — suitable.", **overrides})


NOTES = ("## Breath Pacing With a Word\n\n**Description:** Pacing the breath — then leading. ZEPHYRANCHOR marks "
         "this note.\n\n**How to use it in a session:** Describe the breath, then suggest calm.\n\n**Example "
         "wording:** \"With each breath out, a little more ease.\"\n\n**Functional category:** Deepening\n\n"
         "**Where in the book:** Chapter 1")


def test_a_book_is_read_checked_turned_into_notes_and_used_only_after_approval(client, fake):
    service = client.app.state.service
    pdf = make_pdf()
    added = upload(client, pdf)
    assert added.status_code == 200 and added.json()["status"] == "reading"
    [book] = books(client)
    assert (book["status"], book["pages"], book["parts"], book["filename"]) == ("read", 3, 1, "Trance Craft.pdf")
    assert book["words"] > 300 and book["estimate"]["requests"] == 2 and book["estimate"]["usd"] > 0
    assert not list((service.settings.data_dir / "library" / "uploads").iterdir())  # the PDF itself is not kept
    again = upload(client, pdf)  # the same file again
    assert again.status_code == 409 and again.json()["detail"] == "This book has already been added."

    fake.queue_parse("BookCheck", suitable())
    fake.completions.append((NOTES, "end_turn"))
    started = client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH)
    assert started.status_code == 200
    detail = client.get(f"/api/clinician/books/{book['id']}", auth=CLINICIAN_AUTH).json()
    assert (detail["status"], detail["category"], detail["sections"]) == ("review", "core-craft", 1)
    assert detail["title"] == "Trance Craft" and detail["reason"] == "A clinical hypnotherapy manual, suitable."
    assert "ZEPHYRANCHOR" in detail["notes"] and EM_DASH not in detail["notes"]
    check, notes = [c for c in fake.calls if c["kind"] in ("parse", "complete") and "book" in str(c["messages"])]
    assert "Trance Craft.pdf" in check["messages"][0]["content"] and "pacing what is already happening" in \
        check["messages"][0]["content"]
    assert "part=\"1 of 1\"" in notes["messages"][0]["content"] and "technique notes" in notes["system"][0]["text"]
    assert service.kb.get().search("ZEPHYRANCHOR") == []  # not used before the practice approves it

    assert client.post(f"/api/clinician/books/{book['id']}/approve", auth=CLINICIAN_AUTH).status_code == 200
    [found] = service.kb.get().search("ZEPHYRANCHOR")
    assert found.source_path == f"library/book-{book['id']}.md"
    assert "Trance Craft (A. Author)" in service.books.notes_path(book["id"]).read_text(encoding="utf-8")
    assert books(client)[0]["status"] == "approved"
    status = client.get("/api/clinician/knowledge", auth=CLINICIAN_AUTH).json()
    assert f"library/book-{book['id']}.md" not in status["blocked_files"]

    assert client.delete(f"/api/clinician/books/{book['id']}", auth=CLINICIAN_AUTH).status_code == 200
    assert service.kb.get().search("ZEPHYRANCHOR") == [] and books(client) == []
    assert not service.books.notes_path(book["id"]).exists()


def test_books_of_covert_or_stage_technique_are_refused(client, fake):
    upload(client, make_pdf())
    [book] = books(client)
    fake.queue_parse("BookCheck", suitable(category="excluded", reason="Teaches covert hypnosis on strangers."))
    client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH)
    [refused] = books(client)
    assert (refused["status"], refused["reason"]) == ("refused", "Teaches covert hypnosis on strangers.")
    assert not [c for c in fake.calls if c["kind"] == "complete"]  # no notes written from it
    assert client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH).status_code == 409
    assert client.post(f"/api/clinician/books/{book['id']}/approve", auth=CLINICIAN_AUTH).status_code == 409


def test_only_readable_pdfs_are_accepted(client, monkeypatch):
    assert upload(client, b"just some text", "notes.txt").status_code == 422
    assert upload(client, b"just some text", "fake.pdf").json()["detail"] == "This file is not a PDF."
    upload(client, make_pdf(pages=2, text=""), "Scanned.pdf")
    [scanned] = books(client)
    assert scanned["status"] == "unreadable" and "scanned pictures" in scanned["error"]
    monkeypatch.setattr("app.main.MAX_UPLOAD_BYTES", 1000)
    too_big = upload(client, make_pdf(), "Big.pdf")
    assert too_big.status_code == 413 and "larger than" in too_big.json()["detail"]
    service = client.app.state.service
    assert not list((service.settings.data_dir / "library" / "uploads").iterdir())


def test_an_interrupted_book_carries_on_from_its_last_finished_part(client, fake, monkeypatch):
    monkeypatch.setattr("app.books.PART_CHARS", 2000)  # one page each
    upload(client, make_pdf(pages=3))
    [book] = books(client)
    assert book["parts"] == 3
    fake.queue_parse("BookCheck", suitable())
    fake.completions += [("## Part One Technique\n\n**Description:** one.", "end_turn"),
                         LLMUsageLimit("limit reached")]
    client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH)
    [stopped] = books(client)
    assert (stopped["status"], stopped["parts_done"]) == ("failed", 1)
    assert "usage limit" in stopped["error"] and "carries on" in stopped["error"]
    assert stopped["estimate"]["requests"] == 4

    fake.completions += [("NO_NOTES", "end_turn"), ("# Part Three Technique\n\n**Description:** three.", "end_turn")]
    assert client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH).status_code == 200
    detail = client.get(f"/api/clinician/books/{book['id']}", auth=CLINICIAN_AUTH).json()
    assert detail["status"] == "review" and detail["parts_done"] == 3
    assert detail["notes"] == ("## Part One Technique\n\n**Description:** one.\n\n## Part Three Technique\n\n"
                               "**Description:** three.")
    assert len([c for c in fake.calls if c.get("model") == "BookCheck"]) == 1  # checked once only


def test_a_failed_ai_request_can_be_tried_again(client, fake):
    upload(client, make_pdf())
    [book] = books(client)
    fake.queue_parse("BookCheck", LLMUnavailable("down"))
    client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH)
    [failed] = books(client)
    assert failed["status"] == "failed" and "Press Try again" in failed["error"]
    fake.queue_parse("BookCheck", suitable())
    fake.completions.append(("NO_NOTES", "end_turn"))
    client.post(f"/api/clinician/books/{book['id']}/start", auth=CLINICIAN_AUTH)
    [nothing] = books(client)
    assert (nothing["status"], nothing["sections"]) == ("review", 0)
    refused = client.post(f"/api/clinician/books/{book['id']}/approve", auth=CLINICIAN_AUTH)
    assert refused.status_code == 409 and "nothing" in refused.json()["detail"]


def test_a_restart_stops_work_in_progress_so_it_can_be_tried_again(make_client, fake):
    first = make_client()
    upload(first, make_pdf())
    [book] = books(first)
    first.app.state.service.db.move_book(book["id"], ("read",), "writing", category="core-craft", parts_done=0,
                                         notes="")
    second = make_client()  # the app starting again on the same data
    [after] = books(second)
    assert after["status"] == "failed" and "restarted" in after["error"]
    assert second.delete(f"/api/clinician/books/{book['id']}", auth=CLINICIAN_AUTH).status_code == 200


def test_books_are_for_the_signed_in_admin_only(client):
    assert upload(client, make_pdf(), auth=None).status_code == 401
    assert client.get("/api/clinician/books").status_code == 401


def test_notes_keep_only_technique_sections_without_em_dashes():
    assert clean_notes("NO_NOTES") == "" and clean_notes("  ") == ""
    assert clean_notes("Here are the notes.\n\n# Anchor\n\nText — more.") == "## Anchor\n\nText, more."
    assert clean_notes("## A\n\nx\n\n## B\n\ny") == "## A\n\nx\n\n## B\n\ny"


def test_a_book_is_split_between_paragraphs():
    text = "\n\n".join(["word " * 50] * 10)  # ten paragraphs of 250 characters
    parts = split_parts(text, size=600)
    assert len(parts) == 5 and all(len(p) <= 600 for p in parts)
    assert "\n\n".join(parts).split() == text.split()
    long_paragraph = "word " * 400
    assert all(len(p) <= 600 for p in split_parts(long_paragraph, size=600))


def test_the_cost_estimate():
    priced = estimate(40_000, 1, (4.0, 20.0))  # $4 / $20 per million tokens in / out
    assert priced == {"requests": 2, "input_tokens": 19_500, "output_tokens": 3_000, "usd": 0.14}
    assert estimate(40_000, 1, None)["usd"] is None  # the test engine: the subscription, no price


def test_books_are_read_by_the_cheaper_model_with_plain_requests(settings, kb_root):
    from app.llm import ClaudeGateway
    from app.service import Service

    service = Service(settings, ClaudeGateway("claude-opus-5-5"))
    books = service.books.gateway
    assert books.model == "claude-haiku-4-5" and service.books.prices == (1.0, 5.0)
    request = books._request(system=[], messages=[], effort="medium", max_tokens=16000)
    assert request == {"model": "claude-haiku-4-5", "max_tokens": 16000, "system": [], "messages": []}
    assert "thinking" in service.gateway._request(system=[], messages=[], effort="medium", max_tokens=10)
    # a 300-page book costs well under a dollar
    assert estimate(600_000, 15, service.books.prices)["usd"] < 0.5
