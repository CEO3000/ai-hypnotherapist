import email
import time
from datetime import datetime, timedelta
from email import policy

from tests.conftest import CLINICIAN_AUTH
from tests.support import valid_script
from tests.test_api import CONSULTATION_PAYMENT, choose_ai, clear_answers, consult_to_choice, intake, start, view
from tests.test_session_audio import wait_for_audio
from tests.test_speech import voice_app  # noqa: F401 - voice_app is a fixture


def outbox(settings) -> list:
    """The emails saved instead of sent (no mailbox is set up in tests), oldest first."""
    return [email.message_from_bytes(path.read_bytes(), policy=policy.default)
            for path in sorted((settings.data_dir / "outbox").glob("*.eml"))]


def body(message) -> str:
    return message.get_content()


def preview_time(service, session_id: str) -> datetime:
    """When the preview was made (saved just after its file appears, so wait for it)."""
    for _ in range(200):
        made = service.db.get_session(session_id)["preview_ready_at"]
        if made:
            return datetime.fromisoformat(made)
        time.sleep(0.02)
    raise AssertionError("the preview's time was never saved")


def detail(client, session_id: str) -> dict:
    return client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()


def test_receipts_carry_the_private_link_and_the_client_is_told_when_the_session_is_ready(voice_app, fake, settings):
    client, _ = voice_app(require_clinician_review=False)
    session_id = start(client)["session_id"]
    [receipt] = outbox(settings)
    assert receipt["To"] == "sam.client@example.com" and receipt["Reply-To"] == "practice@example.com"
    assert receipt["Subject"] == "Your receipt: Personal hypnotherapy consultation"
    text = body(receipt)
    assert "£5.00" in text and "TEST VERSION: no money was taken." in text and "(UK time)" in text
    assert f"{settings.public_url}/#s={session_id}\n" in text  # their way back, on any device
    assert "I lose my 14-day right to cancel once the consultation has been completed" in text
    assert "stop-emails" not in text  # a receipt is part of what they bought, not a reminder

    consult_to_choice(client, fake, session_id)
    fake.completions.append((valid_script(), "end_turn"))
    choose_ai(client, session_id, 6)
    wait_for_audio(client, session_id)
    for _ in range(100):  # the "ready" email is sent by the audio worker right after the file is made
        if len(outbox(settings)) == 4:
            break
        time.sleep(0.05)
    subjects = [message["Subject"] for message in outbox(settings)]
    assert sorted(subjects) == sorted(["Your receipt: Personal hypnotherapy consultation",
                                       "Your free session preview is ready",  # they need not wait on the page
                                       "Your receipt: Full personalised AI hypnotherapy session",
                                       "Your hypnotherapy session is ready"])
    preview_email = next(m for m in outbox(settings) if m["Subject"] == "Your free session preview is ready")
    assert f"{settings.public_url}/#s={session_id}" in body(preview_email)
    by_subject = {m["Subject"]: m for m in outbox(settings)}
    session_receipt = by_subject["Your receipt: Full personalised AI hypnotherapy session"]
    ready = by_subject["Your hypnotherapy session is ready"]
    assert "£35.00" in body(session_receipt) and "once its supply begins" in body(session_receipt)
    assert f"#s={session_id}" in body(ready) and "available for 7 days" in body(ready)
    assert sorted(e["kind"] for e in detail(client, session_id)["emails"]) == sorted([
        "receipt_consultation", "preview_ready", "receipt_session", "session_ready"])


def test_when_the_hypnotherapist_is_advised_the_client_hears_it_by_email(client, fake, settings):
    # The page tells them they need not wait for the summary, so the outcome reaches them by email too (once).
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id, intake(hypnotherapist_issue="phobia"))
    sent = [m for m in outbox(settings) if m["Subject"] == "Your consultation: your next step"]
    assert len(sent) == 1
    assert session_id in body(sent[0]) and "qualified hypnotherapist" in body(sent[0])
    assert "preview" not in body(sent[0])  # nothing is promised that will not come
    assert "your £5 consultation fee is not refunded" in body(sent[0])  # and why
    assert "next_step" in {e["kind"] for e in detail(client, session_id)["emails"]}


def test_for_a_safety_reason_the_email_says_to_see_a_doctor_not_the_hypnotherapist(client, fake, settings):
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id, intake(
        suitability_for_self_guided_session={"suitable": False, "reasons": ["epilepsy came up"]}))
    [sent] = [m for m in outbox(settings) if m["Subject"] == "Your consultation: please speak to your doctor first"]
    assert "GP or doctor" in body(sent) and "hypnotherapist" not in body(sent)
    assert "your £5 consultation fee is not refunded" in body(sent)


def test_an_undecided_preview_gets_at_most_two_reminders_and_none_once_stopped(voice_app, fake, settings):
    client, _ = voice_app(require_clinician_review=False)
    service = client.app.state.service
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    wait_for_audio(client, session_id, "preview_ready")
    made = preview_time(service, session_id)
    assert service.send_due_emails(made + timedelta(hours=23)) == []
    assert service.send_due_emails(made + timedelta(days=1, hours=1)) == [(session_id, "preview_reminder_1")]
    assert service.send_due_emails(made + timedelta(days=2)) == []  # never the same reminder twice
    assert service.send_due_emails(made + timedelta(days=6, hours=1)) == [(session_id, "preview_reminder_2")]
    assert service.send_due_emails(made + timedelta(days=6, hours=2)) == []
    reminders = [m for m in outbox(settings) if m["Subject"] == "Your session preview is waiting"]
    assert len(reminders) == 2
    assert "removed in 6 days" in body(reminders[0]) and "removed tomorrow" in body(reminders[1])
    assert f"{settings.public_url}/#s={session_id}&stop-emails" in body(reminders[0])

    late = start(client)["session_id"]  # the app was off for days: only the latest reminder, no burst
    consult_to_choice(client, fake, late)
    wait_for_audio(client, late, "preview_ready")
    made = preview_time(service, late)
    assert service.send_due_emails(made + timedelta(days=6, hours=1)) == [(late, "preview_reminder_2")]
    assert service.send_due_emails(made + timedelta(days=6, hours=2)) == []

    stopped = start(client)["session_id"]  # the client stops them from the link in an email
    consult_to_choice(client, fake, stopped)
    wait_for_audio(client, stopped, "preview_ready")
    assert client.post(f"/api/sessions/{stopped}/stop-emails").status_code == 200
    assert view(client, stopped)["emails_ok"] is False
    declined = client.post("/api/sessions", json={"country": "GB", "language": "en", "answers": clear_answers()})
    declined_id = declined.json()["session_id"]  # the client said no to these emails when paying
    client.post(f"/api/sessions/{declined_id}/pay", json={**CONSULTATION_PAYMENT, "emails_ok": False})
    consult_to_choice(client, fake, declined_id)
    wait_for_audio(client, declined_id, "preview_ready")
    later = preview_time(service, declined_id) + timedelta(days=1, hours=1)
    assert [sent for sent in service.send_due_emails(later) if sent[0] in (stopped, declined_id)] == []


def test_the_follow_up_asks_how_they_are_ten_days_after_the_session(voice_app, fake, settings):
    client, _ = voice_app(require_clinician_review=False)
    service = client.app.state.service
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    fake.completions.append((valid_script(), "end_turn"))
    choose_ai(client, session_id, 7)
    delivered = datetime.fromisoformat(service.db.get_session(session_id)["delivered_at"])
    assert service.send_due_emails(delivered + timedelta(days=9)) == []
    assert service.send_due_emails(delivered + timedelta(days=10, minutes=1)) == [(session_id, "follow_up")]
    assert service.send_due_emails(delivered + timedelta(days=11)) == []
    [message] = [m for m in outbox(settings) if m["Subject"] == "How are you doing?"]
    assert f"{settings.public_url}/#s={session_id}&followup" in body(message)
    assert "stop-emails" in body(message)

    assert view(client, session_id)["follow_up_given"] is False
    answer = f"/api/sessions/{session_id}/follow-up"
    assert client.post(answer, json={"severity": 11}).status_code == 422
    assert client.post(answer, json={"severity": 3, "comments": "Much calmer at work."}).status_code == 200
    assert client.post(answer, json={"severity": 2}).status_code == 409
    assert view(client, session_id)["follow_up_given"] is True
    followup = detail(client, session_id)["followup"]
    assert (followup["severity"], followup["comments"]) == (3, "Much calmer at work.")
    assert detail(client, session_id)["session"]["baseline_severity"] == 7  # before -> after, side by side


def test_a_follow_up_is_only_for_a_delivered_session(client):
    session_id = start(client)["session_id"]
    assert client.post(f"/api/sessions/{session_id}/follow-up", json={"severity": 3}).status_code == 409


def test_without_the_mailbox_password_emails_are_kept_in_the_outbox_not_sent(settings, tmp_path, monkeypatch):
    from dataclasses import replace

    import app.notify as notify

    def never(*args, **kwargs):
        raise AssertionError("no connection may be made without the password")

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", never)
    monkeypatch.setattr(notify.smtplib, "SMTP", never)
    zoho = replace(settings, smtp_host="smtppro.zoho.eu", smtp_port=465, smtp_user="practice@example.com",
                   smtp_password="")
    message = notify.session_ready_email(zoho, to="sam@example.com", session_id="x" * 32, days=7)
    assert notify.send(zoho, message, tmp_path / "outbox") == "saved"
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1
