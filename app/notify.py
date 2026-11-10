"""Emails, sent through the practice's own mailbox by SMTP, so there is no outside service and no fee: to the
practice (a client asking for a qualified hypnotherapist) and to clients (receipts with their private link back to
their session, "your session is ready", preview reminders and the follow-up). Until the mailbox's SMTP details are
set in .env, each email is saved to data/outbox as an .eml file instead (it opens in any mail program)."""

import json
import logging
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import CONTENT_DIR, Settings

log = logging.getLogger(__name__)
UK_TIME = ZoneInfo("Europe/London")  # receipts show UK time, summer and winter (needs tzdata on Windows)
# The client's emails in each language they can choose (emails to the practice are always in English).
CLIENT_TEXTS = json.loads((CONTENT_DIR / "emails.json").read_text(encoding="utf-8"))


def texts(language: str) -> dict:
    return CLIENT_TEXTS.get(language, CLIENT_TEXTS["en"])


def fill(lines: list[str], **values) -> list[str]:
    """The template's lines with the values put in; a line that is only an empty value is left out."""
    out = []
    for line in lines:
        text = line
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        if text or not line:
            out.append(text)
    return out


def human_request_email(settings: Settings, *, session_id: str, name: str, email: str, phone: str,
                        description: str, summary: dict | None, transcript_pdf: bytes | None = None,
                        questions: int = 0, language: str = "English") -> EmailMessage:
    """The client's request for a qualified hypnotherapist, with their AI consultation (every question and answer)
    attached as a PDF when they had one."""
    message = EmailMessage()
    message["Subject"] = f"New hypnotherapist request: {name}"
    message["From"] = settings.smtp_from or settings.smtp_user or settings.notify_email
    message["To"] = settings.notify_email
    message["Reply-To"] = email
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=(settings.notify_email.split("@")[-1] or None))
    lines = [
        "A client has asked for a session with a qualified hypnotherapist.",
        "",
        f"Full name: {name}",
        f"Email: {email}",
        f"Mobile: {phone}",
        *([] if language == "English" else [
            "",
            f"Their AI consultation was in {language}. They were told that sessions with our hypnotherapist are in "
            f"English only, so their description and their consultation's answers below may be in {language}."]),
        "",
        "Short description of the problem, in their words:",
        description,
    ]
    if summary:
        lines += ["", "From their AI consultation:",
                  f"- What they'd like help with: {summary.get('presenting_issue') or '-'}",
                  f"- What they'd like instead: {summary.get('goal') or '-'}"]
    if transcript_pdf:
        lines += ["", f"Their AI consultation is attached as a PDF: every question the AI asked ({questions}) and "
                      "every answer they gave."]
    else:
        lines += ["", "They did not have an AI consultation, so there are no questions and answers to attach."]
    lines += ["", f"The session is also in your dashboard: {settings.public_url}/admin "
                  f"(session {session_id[:6]}...)"]
    message.set_content("\n".join(lines))
    if transcript_pdf:
        safe_name = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip() or "client"
        message.add_attachment(transcript_pdf, maintype="application", subtype="pdf",
                               filename=f"Consultation questions and answers - {safe_name}.pdf")
    return message


def session_link(settings: Settings, session_id: str, action: str = "") -> str:
    """The client's private link to their session (the session id is the key: it is long and random)."""
    return f"{settings.client_page()}#s={session_id}" + (f"&{action}" if action else "")


def client_email(settings: Settings, *, to: str, subject: str, lines: list[str], session_id: str,
                 optional: bool, language: str = "en") -> EmailMessage:
    """An email to a client from the practice. Optional emails (reminders, the follow-up) end with a link to stop
    them; receipts and "your session is ready" are part of what the client bought."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from or settings.smtp_user or settings.notify_email
    message["To"] = to
    message["Reply-To"] = settings.notify_email
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=(settings.notify_email.split("@")[-1] or None))
    footer = ["", "--", settings.practice_name, *([settings.practice_address] if settings.practice_address else []),
              settings.notify_email]
    if optional:
        footer += ["", *fill([texts(language)["stop"]], link=session_link(settings, session_id, "stop-emails"))]
    message.set_content("\n".join(lines + footer))
    return message


def receipt_email(settings: Settings, *, to: str, session_id: str, item: str, amount_pence: int, paid_at: datetime,
                  reference: str, acknowledgement: str, test: bool, language: str = "en") -> EmailMessage:
    words = texts(language)
    lines = fill(words["receipt"], item=item, amount=f"{amount_pence / 100:.2f}",
                 date=paid_at.astimezone(UK_TIME).strftime(words["date_format"]), reference=reference,
                 test=words["receipt_test"] if test else "", acknowledgement=acknowledgement,
                 link=session_link(settings, session_id))
    return client_email(settings, to=to, subject=fill([words["receipt_subject"]], item=item)[0], lines=lines,
                        session_id=session_id, optional=False, language=language)


def download_link(settings: Settings, session_id: str) -> str:
    """The direct link to the client's full session MP3 (it downloads the file)."""
    return f"{settings.public_url}/api/sessions/{session_id}/download"


def session_ready_email(settings: Settings, *, to: str, session_id: str, days: int,
                        language: str = "en") -> EmailMessage:
    words = texts(language)
    lines = fill(words["ready"], download=download_link(settings, session_id), link=session_link(settings, session_id),
                 days=days)
    return client_email(settings, to=to, subject=words["ready_subject"], lines=lines, session_id=session_id,
                        optional=False, language=language)


def preview_ready_email(settings: Settings, *, to: str, session_id: str, session_price: int,
                        language: str = "en") -> EmailMessage:
    words = texts(language)
    lines = fill(words["preview_ready"], link=session_link(settings, session_id), price=session_price)
    return client_email(settings, to=to, subject=words["preview_ready_subject"], lines=lines, session_id=session_id,
                        optional=False, language=language)


def next_step_email(settings: Settings, *, to: str, session_id: str, human_from: int, human_to: int, fee: int,
                    language: str = "en") -> EmailMessage:
    """No AI session after the consultation: the practice's hypnotherapist works with this client's issue himself.
    The client was told they need not wait on the page, so they hear it by email too."""
    words = texts(language)
    lines = fill(words["next_step"], link=session_link(settings, session_id), human_from=human_from,
                 human_to=human_to, amount=fee)
    return client_email(settings, to=to, subject=words["next_step_subject"], lines=lines, session_id=session_id,
                        optional=False, language=language)


def next_step_doctor_email(settings: Settings, *, to: str, session_id: str, fee: int,
                           language: str = "en") -> EmailMessage:
    """No AI session after the consultation, for a safety reason: the client should speak to their doctor first."""
    words = texts(language)
    lines = fill(words["next_step_doctor"], amount=fee)
    return client_email(settings, to=to, subject=words["next_step_doctor_subject"], lines=lines,
                        session_id=session_id, optional=False, language=language)


def preview_reminder_email(settings: Settings, *, to: str, session_id: str, days_left: int,
                           session_price: int, language: str = "en") -> EmailMessage:
    words = texts(language)
    when = words["reminder_tomorrow"] if days_left <= 1 else fill([words["reminder_in_days"]], days=days_left)[0]
    lines = fill(words["reminder"], link=session_link(settings, session_id), price=session_price, when=when)
    return client_email(settings, to=to, subject=words["reminder_subject"], lines=lines, session_id=session_id,
                        optional=True, language=language)


def sign_in_alert_email(settings: Settings, *, failures: int) -> EmailMessage:
    """To the practice: someone is trying passwords on the admin dashboard (see admin_auth.py)."""
    message = EmailMessage()
    message["Subject"] = "Security alert: wrong passwords on your admin dashboard"
    message["From"] = settings.smtp_from or settings.smtp_user or settings.notify_email
    message["To"] = settings.notify_email
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=(settings.notify_email.split("@")[-1] or None))
    message.set_content("\n".join([
        f"There have been {failures} wrong sign-in attempts on your admin dashboard in the last 15 minutes.",
        "",
        "None of them got in. Sign-in is now paused for 15 minutes for every internet address that has never "
        "signed in successfully (yours still works if you have signed in from it before).",
        "",
        "If this was not you, no action is needed. If it keeps happening, choose a new password with:",
        "python -m app.set_admin_password",
        "",
        f"{settings.public_url}/admin",
    ]))
    return message


def follow_up_email(settings: Settings, *, to: str, session_id: str, days: int, language: str = "en") -> EmailMessage:
    words = texts(language)
    lines = fill(words["follow_up"], days=days, link=session_link(settings, session_id, "followup"))
    return client_email(settings, to=to, subject=words["follow_up_subject"], lines=lines, session_id=session_id,
                        optional=True, language=language)


def send(settings: Settings, message: EmailMessage, outbox: Path) -> str:
    """Sends the email (or saves it to the outbox when no mailbox is set up). Returns "sent" or "saved"."""
    if settings.smtp_host and settings.smtp_user and not settings.smtp_password:
        log.warning("SMTP_PASSWORD is not set in .env yet: emails are saved to the outbox instead of being sent")
    if not settings.smtp_host or (settings.smtp_user and not settings.smtp_password):
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        path = outbox / f"{stamp}.eml"
        path.write_bytes(bytes(message))
        log.info("Email to %s saved to %s (no SMTP_HOST set in .env)", message["To"], path)
        return "saved"
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if settings.smtp_port != 465:
            server.starttls(context=context)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)
    log.info("Email sent to %s", message["To"])
    return "sent"
