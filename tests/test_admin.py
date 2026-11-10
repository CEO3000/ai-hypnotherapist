"""The admin side: signing in (email, password, robot check), prices, the practice's own instructions to the AI,
the consultation PDF sent with a hypnotherapist request, and the rule that no em dash reaches anyone."""

import email
import time
from datetime import datetime, timedelta, timezone
from email import policy

import pytest

from app.admin_auth import (COOKIE, RobotCheck, check_password, hash_password, password_problems)
from app.config import APP_DIR
from app.service import no_em_dashes
from tests.conftest import (ADMIN_EMAIL, ADMIN_HEADER, ADMIN_PASSWORD, CLINICIAN_AUTH, solve_robot_check)
from tests.support import turn, valid_script
from tests.test_api import HUMAN, consult_to_choice, intake, send, start, view

EM_DASH = "—"


def sign_in(client, *, address=ADMIN_EMAIL, password=ADMIN_PASSWORD, robot=True, header=True):
    solution = solve_robot_check(client.get("/api/admin/robot-check").json()) if robot is True else robot
    return client.post("/api/admin/sign-in", json={"email": address, "password": password, "robot": solution},
                       headers=ADMIN_HEADER if header else {})


def age_sign_ins(client, minutes: int) -> None:
    """As if the recorded sign-in attempts happened this many minutes earlier."""
    with client.app.state.service.db.connect() as conn:
        for row in conn.execute("SELECT id, at FROM admin_sign_ins").fetchall():
            earlier = datetime.fromisoformat(row["at"]) - timedelta(minutes=minutes)
            conn.execute("UPDATE admin_sign_ins SET at = ? WHERE id = ?", (earlier.isoformat(timespec="seconds"),
                                                                          row["id"]))


# --- signing in ------------------------------------------------------------------------------------------------


def test_the_admin_area_needs_signing_in_with_email_password_and_robot_check(client, settings):
    assert client.get("/api/clinician/sessions").status_code == 401
    page = client.get("/admin", follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (303, "/sign-in")
    old = client.get("/clinician", follow_redirects=False)
    assert (old.status_code, old.headers["location"]) == (308, "/admin")
    sign_in_page = client.get("/sign-in")
    assert sign_in_page.status_code == 200 and 'id="robot"' in sign_in_page.text
    assert sign_in_page.headers["x-robots-tag"] == "noindex, nofollow"
    assert sign_in_page.headers["cache-control"] == "no-store"

    wrong_password = sign_in(client, password="Calm-harbour-lantern-48")
    wrong_email = sign_in(client, address="someone@example.com")
    assert wrong_password.status_code == wrong_email.status_code == 401
    assert wrong_password.json() == wrong_email.json() == {"detail": "The email or password is not right."}
    assert COOKIE not in wrong_password.cookies
    assert sign_in(client, robot=None).status_code == 400
    assert sign_in(client, robot={"salt": "x.9999999999", "challenge": "0" * 64, "number": 1,
                                  "signature": "f" * 64}).status_code == 400  # not one the server signed
    assert sign_in(client, header=False).status_code == 403  # another site cannot sign someone in

    check = solve_robot_check(client.get("/api/admin/robot-check").json())
    signed = client.post("/api/admin/sign-in", headers=ADMIN_HEADER,
                         json={"email": " Admin@Example.com ", "password": ADMIN_PASSWORD, "robot": check})
    assert signed.status_code == 200
    cookie = signed.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=43200" in cookie
    assert ("secure" in cookie) == settings.public_url.startswith("https://")  # always over https
    again = client.post("/api/admin/sign-in", headers=ADMIN_HEADER,
                        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "robot": check})
    assert again.status_code == 400  # each robot check works once

    assert client.get("/api/clinician/sessions").status_code == 200
    assert client.get("/admin").status_code == 200
    assert client.get("/sign-in", follow_redirects=False).headers["location"] == "/admin"
    voice = {"voice_name": "", "rate": 0.9, "pitch": 0.9, "session_rate": 0.8}
    assert client.put("/api/clinician/voice", json=voice).status_code == 403  # changes need the dashboard's header
    assert client.put("/api/clinician/voice", json=voice, headers=ADMIN_HEADER).status_code == 200

    assert client.post("/api/admin/sign-out", headers=ADMIN_HEADER).status_code == 200
    assert client.get("/api/clinician/sessions").status_code == 401
    with client.app.state.service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_sign_in_is_closed_until_the_admin_password_is_set(make_client):
    closed = make_client(admin_password_hash="")
    refused = sign_in(closed)
    assert refused.status_code == 503 and "set_admin_password" in refused.json()["detail"]
    assert closed.get("/api/clinician/sessions").status_code == 401


def test_five_wrong_passwords_lock_that_address_out_for_fifteen_minutes(client):
    for attempt in range(4):
        assert sign_in(client, password=f"wrong-{attempt}").status_code == 401
    fifth = sign_in(client, password="wrong-5")
    assert fifth.status_code == 429 and "15 minutes" in fifth.json()["detail"]
    locked = sign_in(client)  # even the right password
    assert locked.status_code == 429 and "Too many attempts" in locked.json()["detail"]
    age_sign_ins(client, 16)
    assert sign_in(client).status_code == 200


def test_many_wrong_passwords_from_anywhere_pause_sign_in_and_alert_the_practice(client, settings):
    db = client.app.state.service.db
    for n in range(19):  # from many addresses: each stays under its own limit
        db.add_sign_in(ip=f"203.0.113.{n}", ok=False, user_agent="bot")
    assert sign_in(client, password="wrong").status_code == 429  # the 20th: paused for unknown addresses
    alerts = [email.message_from_bytes(p.read_bytes(), policy=policy.default)
              for p in (settings.data_dir / "outbox").glob("*.eml")]
    assert [m["Subject"] for m in alerts] == ["Security alert: wrong passwords on your admin dashboard"]
    assert "20 wrong sign-in attempts" in alerts[0].get_content() and alerts[0]["To"] == settings.notify_email
    assert sign_in(client).status_code == 429  # this address never signed in before
    db.add_sign_in(ip="testclient", ok=True, user_agent="the admin's own browser")
    assert sign_in(client).status_code == 200  # an address that has signed in before still can


def test_a_signed_in_session_ends_when_unused_or_when_the_password_changes(make_client, settings):
    first = make_client()
    assert sign_in(first).status_code == 200
    token = first.cookies[COOKIE]
    db = first.app.state.service.db
    with db.connect() as conn:
        conn.execute("UPDATE admin_sessions SET last_seen_at = ?",
                     ((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds"),))
    assert first.get("/api/clinician/sessions").status_code == 401  # unused for more than 2 hours
    assert sign_in(first).status_code == 200
    changed = make_client(admin_password_hash=hash_password("Another-strong-pass-92", n=2**10))
    changed.cookies.set(COOKIE, first.cookies[COOKIE])
    assert changed.get("/api/clinician/sessions").status_code == 401  # the new password signs everyone out
    assert token != first.cookies[COOKIE]


def test_passwords_are_kept_only_as_slow_hashes():
    stored = hash_password("Calm-harbour-lantern-47", n=2**10)
    assert stored.startswith("scrypt:1024:8:1:") and "Calm" not in stored and "$" not in stored
    assert check_password("Calm-harbour-lantern-47", stored)
    assert not check_password("calm-harbour-lantern-47", stored)
    assert not check_password("anything", "not a hash") and not check_password("anything", "")
    assert hash_password("same-Password-1") != hash_password("same-Password-1")  # a new salt every time


@pytest.mark.parametrize("weak", ["short1A!", "alllowercaseletters", "Password-Strong-1", "Hypnosis-2026!",
                                  "aaaaaaaaaaaaAAAA1111"])
def test_weak_admin_passwords_are_refused(weak):
    assert password_problems(weak, "practice@example.com")


def test_strong_admin_passwords_are_accepted():
    assert password_problems("Calm-harbour-lantern-47", "practice@example.com") == []
    assert password_problems("quiet river under a silver moon", "practice@example.com") == []


def test_the_robot_check_must_be_solved_signed_unexpired_and_unused(monkeypatch):
    robot = RobotCheck(max_number=50)
    check = robot.challenge()
    solved = solve_robot_check(check)
    assert not robot.verify({**solved, "number": (solved["number"] + 1) % 51})  # the wrong number
    assert not robot.verify({**solved, "signature": "0" * 64})
    assert not robot.verify("not a solution")
    assert robot.verify(solved) and not robot.verify(solved)
    later = solve_robot_check(robot.challenge())
    in_eleven_minutes = time.time() + 660
    monkeypatch.setattr("app.admin_auth.time.time", lambda: in_eleven_minutes)
    assert not robot.verify(later)  # expired after 10 minutes


# --- prices ----------------------------------------------------------------------------------------------------


def test_the_admin_sets_the_prices_and_clients_pay_what_they_were_shown(client):
    session_id = start(client, pay=False)["session_id"]
    assert view(client, session_id)["payment"]["amount"] == 5
    admin = client.get("/api/clinician/admin", auth=CLINICIAN_AUTH).json()
    assert admin["prices"] == admin["default_prices"] == {"consultation": 5, "ai_session": 35, "human_from": 199,
                                                          "human_to": 299}
    new = {"consultation": 7, "ai_session": 39, "human_from": 180, "human_to": 260}
    assert client.put("/api/clinician/prices", json={**new, "human_from": 300}, auth=CLINICIAN_AUTH).status_code == 422
    assert client.put("/api/clinician/prices", json={**new, "consultation": 0}, auth=CLINICIAN_AUTH).status_code == 422
    assert client.put("/api/clinician/prices", json=new, auth=CLINICIAN_AUTH).status_code == 200
    assert client.get("/api/config").json()["prices"] == {"currency": "GBP", "consultation": 7, "ai_session": 39,
                                                          "total": 46, "human_session_from": 180,
                                                          "human_session_to": 260}
    published = client.get("/api/prices")  # what the practice's website page shows
    assert published.json() == {"currency": "GBP", "consultation": 7, "ai_session": 39, "total": 46,
                                "human_session_from": 180, "human_session_to": 260}
    assert published.headers["cache-control"] == "public, max-age=60"
    stale = client.post(f"/api/sessions/{session_id}/pay", json={
        "purpose": "consultation", "acknowledged": True, "email": "sam.client@example.com", "amount": 5})
    assert stale.status_code == 409 and "price has changed" in stale.json()["detail"]  # the page showed £5
    assert view(client, session_id)["payment"]["amount"] == 7
    paid = client.post(f"/api/sessions/{session_id}/pay", json={
        "purpose": "consultation", "acknowledged": True, "email": "sam.client@example.com", "amount": 7})
    assert paid.status_code == 200
    payments = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()["payments"]
    assert [p["amount_pence"] for p in payments] == [700]


def test_the_preview_reminder_quotes_the_current_session_price(client, fake, settings):
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    assert client.put("/api/clinician/prices", json={"consultation": 5, "ai_session": 42, "human_from": 199,
                                                     "human_to": 299}, auth=CLINICIAN_AUTH).status_code == 200
    service = client.app.state.service
    made = datetime.now(timezone.utc) - timedelta(days=2)
    service.db.set_session_fields(session_id, preview_ready_at=made.isoformat(timespec="seconds"))
    assert service.send_due_emails() == [(session_id, "preview_reminder_1")]
    [reminder] = [m for m in outbox(settings) if m["Subject"] == "Your session preview is waiting"]
    assert "(£42)" in reminder.get_content()


# --- the practice's own instructions to the AI ------------------------------------------------------------------


def test_the_practices_instructions_reach_the_consultant_and_the_script_writer(client, fake):
    service = client.app.state.service
    assert "<practice_instructions>" not in service.consultation_system("en")[0]["text"]
    saved = client.put("/api/clinician/instructions", auth=CLINICIAN_AUTH, json={
        "consultation": "  Always ask how they sleep.  ", "script": "Use gentle nature imagery."})
    assert saved.status_code == 200 and saved.json()["instructions"]["consultation"] == "Always ask how they sleep."
    assert client.put("/api/clinician/instructions", auth=CLINICIAN_AUTH,
                      json={"consultation": "x" * 6001, "script": ""}).status_code == 422
    system = service.consultation_system("en")[0]["text"]
    assert "<practice_instructions>\nAlways ask how they sleep.\n</practice_instructions>" in system
    assert system.index("<practice_question_sets>") < system.index("<practice_instructions>")
    assert "safety and ethics rules always come first" in system

    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)  # the consultation, then the opening written for the preview
    consultant = [c for c in fake.calls if c.get("model") == "ConsultantTurn"][-1]
    assert "Always ask how they sleep." in str(consultant["system"])
    for _ in range(100):
        writing = [c for c in fake.calls if c["kind"] == "complete"]
        if writing:
            break
        time.sleep(0.05)
    script_prompt = "\n".join(block["text"] for block in writing[0]["system"])
    assert "<practice_instructions>\nUse gentle nature imagery.\n</practice_instructions>" in script_prompt
    assert "Always ask how they sleep." not in script_prompt  # each instruction goes where it belongs
    assert client.get("/api/clinician/admin", auth=CLINICIAN_AUTH).json()["instructions"] == {
        "consultation": "Always ask how they sleep.", "script": "Use gentle nature imagery."}


# --- the consultation PDF with a hypnotherapist request -----------------------------------------------------------


def outbox(settings) -> list:
    return [email.message_from_bytes(path.read_bytes(), policy=policy.default)
            for path in sorted((settings.data_dir / "outbox").glob("*.eml"))]


def test_a_hypnotherapist_request_brings_every_question_and_answer_as_a_pdf(make_client, fake, settings):
    client = make_client(require_clinician_review=False)
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    assert client.post(f"/api/sessions/{session_id}/human-request", json=HUMAN).status_code == 200
    [request] = [m for m in outbox(settings) if m["Subject"].startswith("New hypnotherapist request")]
    text = request.get_body(("plain",)).get_content()
    for field in ("Sam Test", "sam@example.com", "07700 900123", "I get very anxious before exams."):
        assert field in text
    assert "attached as a PDF: every question the AI asked (2)" in text
    [attachment] = list(request.iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == "Consultation questions and answers - Sam Test.pdf"
    pdf = attachment.get_content()
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def test_without_a_consultation_the_request_has_no_pdf(client, settings):
    session_id = start(client, pay=False, pregnant=True)["session_id"]  # routed to a human before any consultation
    assert client.post(f"/api/sessions/{session_id}/human-request", json=HUMAN).status_code == 200
    [request] = [m for m in outbox(settings) if m["Subject"].startswith("New hypnotherapist request")]
    assert not list(request.iter_attachments())
    assert "did not have an AI consultation" in request.get_content()


def test_the_pdf_holds_the_questions_and_answers_in_order():
    from app.transcript_pdf import _plain, consultation_pdf
    pdf = consultation_pdf(practice="Example Practice", client={"name": "Élise <Test>", "email": "e@example.com",
                                                               "phone": "07700 900123"},
                           session_ref="abc123", started_at="2026-09-28T10:00:00+00:00", messages=[
                               {"role": "assistant", "content": "What would you like help with?"},
                               {"role": "patient", "content": "Sleep & worry, it's been hard \U0001F614"},
                               {"role": "assistant", "content": "Thank you, that's everything I need."}])
    assert pdf.startswith(b"%PDF") and b"Consultation: questions and answers" in pdf
    assert _plain("café ’ \U0001F614") == "café ’ ?"  # what the font can't show is marked, never dropped


# --- no em dashes, anywhere ---------------------------------------------------------------------------------------


def test_no_em_dash_in_any_of_the_apps_own_texts():
    files = [p for p in APP_DIR.rglob("*") if p.is_file() and "vendor" not in p.parts and "__pycache__" not in p.parts
             and p.suffix in {".py", ".md", ".json", ".js", ".html", ".css", ".txt"}]
    assert len(files) > 30
    assert [str(p) for p in files if EM_DASH in p.read_text(encoding="utf-8")] == []


@pytest.mark.parametrize("written, shown", [
    ("I was tired — really tired — all week.", "I was tired, really tired, all week."),
    ("word—word", "word, word"),
    ("Yes, — and more", "Yes, and more"),
    ("Done. — Next", "Done. Next"),
    ("— an opening line", "an opening line"),
    ("## PART 1 — The Room", "## PART 1, The Room"),
    ("calm – and slow", "calm, and slow"),
    ("60–85 minutes and £199 – £299", "60–85 minutes and £199 – £299"),
])
def test_em_dashes_the_ai_writes_become_commas(written, shown):
    assert no_em_dashes(written) == shown


def test_the_ai_never_shows_a_client_an_em_dash(client, fake):
    session_id = start(client)["session_id"]
    fake.queue_parse("ConsultantTurn", turn("That sounds hard — when did it start?"))
    events = send(client, session_id, "I want to stop smoking")
    assert events[0] == ("delta", {"text": "That sounds hard, when did it start?"})
    fake.queue_parse("ConsultantTurn", turn("Thank you, Sam.", complete=True, note="Covered."))
    fake.queue_parse("ConsultationIntake", intake(presenting_issue="Smoking — 15 a day",
                                                  goal_in_client_words="to be free — fully"))
    fake.completions.append((valid_script().replace("## PRE-TALK\n", "## PRE-TALK\nBreathe — slowly.\n"),
                             "end_turn"))
    send(client, session_id, "Please call me Sam")
    for _ in range(100):
        current = view(client, session_id)
        if current.get("preview_state") in ("ready", "failed"):
            break
        time.sleep(0.05)
    assert current["summary"]["presenting_issue"] == "Smoking, 15 a day"
    assert EM_DASH not in str(current)
    opening = client.app.state.service.db.latest_opening(session_id)["text"]
    assert "Breathe, slowly." in opening and EM_DASH not in opening


def test_the_ready_email_has_a_direct_download_link(settings):
    from app import notify
    message = notify.session_ready_email(settings, to="sam@example.com", session_id="s" * 32, days=7)
    text = message.get_content()
    assert f"{settings.public_url}/api/sessions/{'s' * 32}/download\n" in text
    assert f"{settings.public_url}/#s={'s' * 32}\n" in text and EM_DASH not in text


def test_the_password_tool_saves_only_a_hash_and_keeps_the_rest_of_env(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    from app import set_admin_password as tool
    env = tmp_path / ".env"
    env.write_text("AI_ENGINE=claude-code\nADMIN_PASSWORD_HASH=scrypt:old\nSMTP_HOST=smtp.zoho.eu\n", encoding="utf-8")
    monkeypatch.setattr(tool, "PROJECT_ROOT", tmp_path)  # a copy: the real .env is never touched

    def typed(*answers):
        replies = iter(answers)
        monkeypatch.setattr(tool, "ask", lambda prompt="": next(replies))

    typed("Calm-harbour-lantern-47", "Calm-harbour-lantern-47")
    assert tool.main() == 0
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["AI_ENGINE=claude-code", "SMTP_HOST=smtp.zoho.eu"] and len(lines) == 3
    saved = dotenv_values(env)["ADMIN_PASSWORD_HASH"]  # read back exactly as the app reads it
    assert check_password("Calm-harbour-lantern-47", saved) and "Calm" not in env.read_text(encoding="utf-8")

    before = env.read_text(encoding="utf-8")
    typed("short")  # too weak: refused before asking again
    assert tool.main() == 1
    typed("Calm-harbour-lantern-47", "Calm-harbour-lantern-48")  # typed differently the second time
    assert tool.main() == 1
    assert env.read_text(encoding="utf-8") == before


def test_the_password_prompt_shows_a_star_for_each_character(monkeypatch, capsys):
    msvcrt = pytest.importorskip("msvcrt")  # the Windows console
    from app.set_admin_password import ask
    keys = iter(["a", "b", "x", "\x08", "\xe0", "K", "c", "\r"])  # x then Backspace; an arrow key (two codes)
    monkeypatch.setattr(msvcrt, "getwch", lambda: next(keys))
    assert ask("Password: ") == "abc"
    shown = capsys.readouterr().out
    assert shown.startswith("Password: ***\b \b*") and "abc" not in shown
