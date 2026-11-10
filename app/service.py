"""Application logic: patient journey, consultation turns, background steps and clinician review."""

import asyncio
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .config import CONTENT_DIR, PROMPTS_DIR, Settings
from .consultation import ConsultantTurn
from .books import Library
from .db import Database
from .payments import CardNeedsCheckout, StripePayments
from .intake import extract_intake
from .knowledge import KnowledgeBaseError, KnowledgeStore, render_documents, sha
from .learning import propose_lessons
from .llm import ClaudeGateway, LLMRefusal, LLMResult, LLMUnavailable, LLMUsageLimit, usage_totals
from .risk import KeywordScreen, assess
from .screening import Screening
from . import notify
from .script_writer import (assemble_script, must_fix, patient_version, render_screening, validate_opening,
                            validate_script, write_script)
from .session_delivery import delivery_plan
from .text_rules import no_em_dashes, without_em_dashes
from .transcript_pdf import consultation_pdf
from .voice_cues import describe as describe_voice

log = logging.getLogger(__name__)

PROMPT_NAMES = ("consultation", "risk_classifier", "intake_extraction", "script_writer", "lessons", "book_check",
                "book_notes")
# Where the client pays, and the status each payment moves the session on to.
PAYMENT_STEPS = {"consultation": ("awaiting_consultation_payment", "consulting"),
                 "session": ("awaiting_session_payment", "generating")}
# Statuses in which the client's session audio (the preview, later the whole session) exists or is being made.
AUDIO_STATUSES = ("awaiting_choice", "awaiting_session_payment", "generating", "ready")
# What the consultant says when the client is quiet: a check-in, the pause, and on their return.
SILENCE_PHRASES = ("silence_check_in", "silence_pause", "silence_resume")
EXTRA_MINUTES = 10  # a consultation that ended with too few answers can be continued once, for this long
ENDED_EARLY = "Ended early by the client."
PAUSE_ABANDONED = timedelta(hours=24)  # a consultation left paused this long is closed
DEFAULT_REVERB = 0.5  # a warm hall, clearly there but never washing the words out
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}

# Claude API list prices, USD per million tokens (input, output). Cache writes bill at 1.25x input
# (5-minute cache), cache reads at 0.1x. For the Claude Code test engine this is what the same traffic
# would cost on the API; the test engine itself runs on the subscription.
PRICES_PER_MTOK = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def stamp() -> str:
    """Now, as stored in the database."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conflict(Exception):
    """The requested action is not valid for the session's current state."""


class Invalid(Exception):
    """The request is missing something the client must give (shown to them)."""




PRICE_KEYS = ("consultation", "ai_session", "human_from", "human_to")
INSTRUCTION_KINDS = ("consultation", "script")


def mask_email(email: str) -> str:
    """sam.taylor@example.com -> sa***@example.com (shown on the page, which anyone with the link can open)."""
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CrisisResources:
    def __init__(self, data: dict):
        self.countries = data["countries"]
        self.default = data["default"]

    @staticmethod
    def _local(texts: dict, language: str) -> str:
        return texts.get(language, texts["en"])

    def options(self, language: str) -> list[dict]:
        options = [{"code": code, "name": self._local(c["name"], language)} for code, c in self.countries.items()]
        options.append({"code": "OTHER", "name": self._local(self.default["name"], language)})
        return options

    def is_valid(self, code: str) -> bool:
        return code in self.countries or code == "OTHER"

    def for_country(self, code: str, language: str) -> list[dict]:
        entry = self.countries.get(code, self.default)
        return [
            {"name": self._local(r["name"], language), "detail": self._local(r["detail"], language)}
            for r in entry["resources"]
        ]


def estimate_cost_usd(rows: list[dict]) -> float | None:
    total = 0.0
    for row in rows:
        prices = PRICES_PER_MTOK.get(row["model"])
        if prices is None:
            return None
        price_in, price_out = prices
        total += (
            row["input_tokens"] * price_in
            + row["output_tokens"] * price_out
            + row["cache_write_tokens"] * price_in * 1.25
            + row["cache_read_tokens"] * price_in * 0.1
        ) / 1_000_000
    return round(total, 4)


def to_api_messages(history: list[dict]) -> list[dict]:
    """Stored transcript -> Messages API turns. The fixed greeting lives in the system prompt; measured voice cues
    travel with the client's answer as a separate, clearly labelled block."""
    messages = []
    for m in history:
        role = "user" if m["role"] == "patient" else "assistant"
        if not messages and role == "assistant":
            continue
        blocks = [{"type": "text", "text": m["content"]}]
        summary = (m.get("voice") or {}).get("summary")
        if role == "user" and summary:
            blocks.append({"type": "text", "text": f"[{summary}]"})
        messages.append({"role": role, "content": blocks})
    if messages:
        messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    return messages


def discard(task: asyncio.Task) -> None:
    """Cancel a speculative task whose result is no longer wanted, without leaving an unread exception."""
    task.cancel()
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


# Stopped mid-consultation for these alone, the client goes to the practice's hypnotherapist (pregnancy; the
# distress of trauma, as with PTSD); for any other safety reason, to a doctor or a safety service.
HYPNOTHERAPIST_STOPS = {"pregnancy", "severe_trauma_distress"}


class Service:
    def __init__(self, settings: Settings, gateway: ClaudeGateway):
        self.settings = settings
        self.gateway = gateway
        self.on_delivered = None  # called with a session id when its opening or its whole script is ready (audio)
        self.db = Database(settings.data_dir / "platform.sqlite3")
        self.kb = KnowledgeStore(settings.knowledge_root, settings.data_dir / "library" / "books")
        self.screening = Screening.from_file(CONTENT_DIR / "screening.json")
        self.crisis = CrisisResources(json.loads((CONTENT_DIR / "crisis_resources.json").read_text(encoding="utf-8")))
        self.keywords = KeywordScreen.from_file(CONTENT_DIR / "risk_patterns.json")
        self.content = json.loads((CONTENT_DIR / "consultation.json").read_text(encoding="utf-8"))
        # The practice's own questions for each issue, asked first (edited by the practice in this file).
        self.question_sets = (CONTENT_DIR / "consultation_questions.md").read_text(encoding="utf-8")
        self.prompts = {name: (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8") for name in PROMPT_NAMES}
        self.prompt_versions = {name: sha(text) for name, text in self.prompts.items()}
        # The languages clients can choose (LANGUAGES in .env), each with its name for the AI's instructions.
        self.languages = {code: self.content["language_names"][code] for code in settings.languages
                          if code in self.content["language_names"]}
        # Books the practice adds in the admin panel: read by a smaller, cheaper model (BOOKS_MODEL) with the
        # pay-as-you-go API; on the test engine the subscription pays, so no cost is shown.
        books_gateway = gateway.with_model(settings.books_model) if hasattr(gateway, "with_model") else gateway
        model = getattr(books_gateway, "model", settings.model)
        # Card payments: Stripe when PAYMENT_PROVIDER=stripe, otherwise the test checkout (no money moves).
        self.payments = StripePayments(secret_key=settings.stripe_secret_key,
                                       webhook_secret=settings.stripe_webhook_secret,
                                       client_page=settings.client_page()) \
            if settings.payment_provider == "stripe" else None
        self.books = Library(db=self.db, gateway=books_gateway, data_dir=settings.data_dir,
                             prompts={name: self.prompts[name] for name in ("book_check", "book_notes")},
                             record=lambda purpose, result: self.record(None, purpose, result),
                             prices=None if getattr(gateway, "engine", "api") == "claude-code"
                             else PRICES_PER_MTOK.get(model))
        self._locks: dict[str, asyncio.Lock] = {}
        self._inflight: set[str] = set()

    def startup(self, carry_on: bool = False) -> None:
        """carry_on: work a restart interrupted (a summary, a preview's opening, a paid session being written) is
        carried on by the app (see interrupted_work); otherwise it is marked failed and the client restarts it."""
        self.db.init(mark_interrupted_failed=not carry_on)
        self.books.startup()
        kb = self.kb.get()
        log.info("Knowledge base loaded: %d library sections, %d files blocked, %d sections dropped",
                 len(kb.chunks), len(kb.blocked_files), len(kb.dropped_sections))

    def record(self, session_id: str | None, purpose: str, result: LLMResult) -> None:
        self.db.add_llm_call(session_id=session_id, purpose=purpose, model=result.model,
                             stop_reason=result.stop_reason, usage=usage_totals(result.usage),
                             request_id=result.request_id)

    # --- patient journey ------------------------------------------------------

    def start_session(self, *, country: str, language: str, answers: dict) -> dict:
        """The safety questions come first: only a client they clear is asked to pay for the consultation."""
        result = self.screening.evaluate(answers)
        session_id = secrets.token_urlsafe(24)
        self.db.create_session(
            session_id=session_id, country=country, language=language,
            status="awaiting_consultation_payment" if result.outcome == "clear" else "screening_stopped",
            screening_version=self.screening.version, screening_answers=answers,
            screening_outcome=result.to_dict(),
        )
        return {"session_id": session_id, **self.patient_view(self.db.get_session(session_id))}

    # --- payments (test checkout for now: the session moves on at once and no money is taken) ---------------

    def price_gbp(self, purpose: str) -> int:
        prices = self.prices()
        return {"consultation": prices["consultation"], "session": prices["ai_session"]}[purpose]

    @property
    def payments_test_mode(self) -> bool:
        """No real money moves: the test checkout, or Stripe with a test key (sk_test_... or restricted rk_test_...)."""
        return self.payments is None or self.settings.stripe_secret_key.startswith(("sk_test_", "rk_test_"))

    def saved_card(self, session_id: str) -> bool:
        return self.db.saved_card(session_id) is not None

    def payment_item(self, purpose: str, language: str) -> str:
        """What is being paid for, in the client's language (on Stripe's page and the receipt)."""
        items = self.content["payment_items"]
        return items.get(language, items["en"])[purpose]

    def acknowledgement(self, purpose: str, language: str) -> str:
        """The words the client agrees to about their right to cancel (UK Consumer Contracts Regulations)."""
        texts = self.content["cancellation_acknowledgement"]
        return texts.get(language, texts["en"])[purpose]

    def pay(self, session: dict, *, purpose: str, save_card: bool, acknowledged: bool, email: str | None = None,
            emails_ok: bool = True, amount_seen: int | None = None) -> dict:
        """The client pays for the consultation (before it starts, giving their email for the receipt and their
        private link) or for their full session (after the preview). Either way they first confirm the words about
        their right to cancel, which are kept with the payment.
        Returns {"paid": True} when the payment is complete (the test checkout, or a saved card charged with
        Stripe), or {"checkout_url": ...}: Stripe's secure payment page, after which confirm_payment completes it."""
        waiting, paid = PAYMENT_STEPS[purpose]
        if session["status"] != waiting:
            raise Conflict("this payment is not due at this stage")
        if amount_seen is not None and amount_seen != self.price_gbp(purpose):
            raise Conflict("the price has changed; please reload the page")  # never a price they did not see
        if not acknowledged:
            raise Invalid("please confirm that you want to start straight away")
        if purpose == "consultation" and not email:
            raise Invalid("please give your email address")
        payment = {"session_id": session["id"], "purpose": purpose, "amount_pence": self.price_gbp(purpose) * 100,
                   "currency": "GBP", "provider": self.settings.payment_provider,
                   "acknowledgement": self.acknowledgement(purpose, session["language"])}
        if self.payments is None:  # the test checkout: paid at once, no money moves
            if not self.db.transition(session["id"], (waiting,), paid):
                raise Conflict("the session changed; please reload")
            if purpose == "consultation":
                self.db.set_session_fields(session["id"], client_email=email, emails_ok=int(emails_ok))
            self.db.add_payment(**payment, status="paid", save_card=save_card,
                                provider_ref=f"test_{secrets.token_hex(6)}")
            if purpose == "consultation":
                self.db.add_message(session["id"], "assistant", self.content["greeting"][session["language"]])
            return {"paid": True}

        if purpose == "consultation":
            self.db.set_session_fields(session["id"], client_email=email, emails_ok=int(emails_ok))
        card = self.db.saved_card(session["id"]) if purpose == "session" else None
        if card and card.get("customer_ref") and card.get("method_ref"):
            payment_id = self.db.add_payment(**payment, status="pending", save_card=False)
            try:
                reference = self.payments.charge_saved_card(
                    customer=card["customer_ref"], payment_method=card["method_ref"],
                    amount_pence=payment["amount_pence"], item=self.payment_item(purpose, session["language"]),
                    session_id=session["id"],
                    purpose=purpose)
            except CardNeedsCheckout as exc:  # their bank wants them to confirm: the payment page instead
                log.info("Session %s: saved card needs the client (%s)", session["id"][:6], exc)
                self.db.mark_payment_failed(payment_id)
            else:
                if not self._complete_payment(session["id"], payment_id, purpose, provider_ref=reference):
                    raise Conflict("the session changed; please reload")
                return {"paid": True}
        checkout = self.payments.checkout(
            session_id=session["id"], purpose=purpose, item=self.payment_item(purpose, session["language"]),
            locale=session["language"],
            amount_pence=payment["amount_pence"], email=(self.db.get_session(session["id"]) or {}).get("client_email"),
            save_card=save_card and purpose == "consultation")
        self.db.add_payment(**payment, status="pending", save_card=save_card and purpose == "consultation",
                            provider_ref=checkout["id"])
        return {"checkout_url": checkout["url"]}

    def _complete_payment(self, session_id: str, payment_id: int, purpose: str, **refs) -> bool:
        """A payment Stripe has confirmed: the session moves on (once, however many times this is called)."""
        waiting, paid = PAYMENT_STEPS[purpose]
        if not self.db.mark_payment_paid(payment_id, **refs):
            return False  # already done (the client's return and Stripe's notice can both arrive)
        if not self.db.transition(session_id, (waiting,), paid):
            log.error("Session %s: payment %s was taken but the session was no longer waiting for it: refund it in "
                      "Stripe", session_id[:6], payment_id)
            return False
        if purpose == "consultation":
            session = self.db.get_session(session_id)
            self.db.add_message(session_id, "assistant", self.content["greeting"][session["language"]])
        return True

    def confirm_payment(self, session_id: str, checkout_id: str) -> str | None:
        """Checks a Stripe payment page with Stripe itself and completes the payment if it was paid. Returns the
        purpose when this call completed it (so the receipt and the next step start), None when it already was."""
        payment = self.db.payment_by_ref(checkout_id)
        if payment is None or payment["session_id"] != session_id or self.payments is None:
            raise Invalid("unknown payment")
        if payment["status"] == "paid":
            return None
        found = self.payments.checkout_status(checkout_id)
        if not found["paid"]:
            raise Conflict("the payment was not completed")
        if (found["session_id"], found["purpose"], found["amount_pence"], (found["currency"] or "").upper()) != (
                session_id, payment["purpose"], payment["amount_pence"], payment["currency"]):
            log.error("Session %s: Stripe payment %s does not match what was asked for", session_id[:6], checkout_id)
            raise Conflict("the payment details do not match; please contact us")
        done = self._complete_payment(session_id, payment["id"], payment["purpose"],
                                      customer_ref=found["customer"], method_ref=found["payment_method"])
        return payment["purpose"] if done else None

    def stripe_notice(self, payload: bytes, signature: str | None) -> tuple[str, str] | None:
        """A notification from Stripe (the webhook): a finished payment page completes its payment even if the
        client closed the page. Returns (session id, purpose) when it completed a payment."""
        if self.payments is None:
            raise Invalid("card payments are not set up")
        event = self.payments.event(payload, signature)  # ValueError if it is not genuine
        if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            return None
        checkout = event["data"]["object"]
        session_id = (checkout.get("metadata") or {}).get("session_id")
        if not session_id or self.db.payment_by_ref(checkout["id"]) is None:
            return None  # not one of ours (for example a payment on the practice's website)
        try:
            purpose = self.confirm_payment(session_id, checkout["id"])
        except Conflict:
            return None  # not paid yet: Stripe sends async_payment_succeeded when it is
        return (session_id, purpose) if purpose else None

    # --- emails to the client -------------------------------------------------------------------------------

    def email_client(self, session_id: str, kind: str, message) -> str:
        """Sends one email to the client and logs it ("sent", "saved" to the outbox, or "failed" - then a copy is
        kept in the outbox)."""
        outbox = self.settings.data_dir / "outbox"
        try:
            status = notify.send(self.settings, message, outbox)
        except Exception:  # noqa: BLE001 - a mail problem must never break the client's journey
            log.exception("Session %s: the %s email could not be sent", session_id[:6], kind)
            notify.send(replace(self.settings, smtp_host=""), message, outbox)
            status = "failed"
        self.db.log_email(session_id=session_id, kind=kind, recipient=message["To"], status=status)
        return status

    def emails_sent(self, session_id: str) -> set[str]:
        return {email["kind"] for email in self.db.list_emails(session_id) if email["status"] != "failed"}

    def send_receipt(self, session_id: str, purpose: str) -> None:
        """The receipt, with the words agreed about cancelling and the client's private link to their session."""
        session = self.db.get_session(session_id)
        payment = next((p for p in reversed(self.db.list_payments(session_id))
                        if p["purpose"] == purpose and p["status"] == "paid"), None)
        if not session or not session.get("client_email") or payment is None:
            return
        message = notify.receipt_email(
            self.settings, to=session["client_email"], session_id=session_id,
            item=self.payment_item(purpose, session["language"]), language=session["language"],
            amount_pence=payment["amount_pence"], paid_at=datetime.fromisoformat(payment["paid_at"]),
            reference=payment["provider_ref"] or str(payment["id"]), acknowledgement=payment["acknowledgement"] or "",
            test=payment["provider"] == "test" or self.payments_test_mode)
        self.email_client(session_id, f"receipt_{purpose}", message)

    def preview_made(self, session_id: str) -> None:
        """Called when the session's preview file is made (the reminders count from then)."""
        session = self.db.get_session(session_id)
        if session and not session.get("preview_ready_at"):
            self.db.set_session_fields(session_id, preview_ready_at=utc_now().isoformat(timespec="seconds"))
        # The client is told by email (once), so they need not wait on the page while it is made.
        if session and session.get("client_email") and "preview_ready" not in self.emails_sent(session_id):
            message = notify.preview_ready_email(self.settings, to=session["client_email"], session_id=session_id,
                                                 session_price=self.price_gbp("session"),
                                                 language=session["language"])
            self.email_client(session_id, "preview_ready", message)

    def send_next_step(self, session_id: str) -> None:
        """No AI session is offered after the consultation: the client is told by email (once) where to go next, the
        practice's hypnotherapist or their doctor, as the page told them they need not wait for it."""
        session = self.db.get_session(session_id)
        if not session or not session.get("client_email") or "next_step" in self.emails_sent(session_id):
            return
        fee = self.consultation_fee(session_id)
        if self.referral(session) == "doctor":
            message = notify.next_step_doctor_email(self.settings, to=session["client_email"], session_id=session_id,
                                                    fee=fee, language=session["language"])
        else:
            prices = self.prices()
            message = notify.next_step_email(self.settings, to=session["client_email"], session_id=session_id,
                                             human_from=prices["human_from"], human_to=prices["human_to"], fee=fee,
                                             language=session["language"])
        self.email_client(session_id, "next_step", message)

    def consultation_fee(self, session_id: str) -> int:
        """What the client paid for their consultation, in whole pounds."""
        return sum(p["amount_pence"] for p in self.db.list_payments(session_id)
                   if p["purpose"] == "consultation" and p["status"] == "paid") // 100

    def referral(self, session: dict) -> str | None:
        """Where a client goes when no AI session is offered (the practice's rule): "hypnotherapist" for what he works
        with himself (PTSD, phobias, stuttering, migraine, panic attacks, pregnancy), "doctor" for any other safety
        reason (a doctor or a safety service, never the hypnotherapist). None while an AI session is possible."""
        if session["status"] == "consultation_stopped":
            stop = session.get("stop_detail") or {}
            if stop.get("level") == "handoff":  # the assistant could not carry on (not about the client's safety)
                return "hypnotherapist"
            if stop.get("level") == "concern":
                categories = set(stop.get("categories") or [])
                return "hypnotherapist" if categories and categories <= HYPNOTHERAPIST_STOPS else "doctor"
            return None
        intake = session.get("intake")
        if intake and session.get("opening_status") == "not_offered":
            return "hypnotherapist" if intake["suitability_for_self_guided_session"]["suitable"] else "doctor"
        return None

    def session_ready(self, session_id: str) -> None:
        """Called when the full session file is made: the client is told by email (once)."""
        session = self.db.get_session(session_id)
        if not session or not session.get("client_email") or "session_ready" in self.emails_sent(session_id):
            return
        message = notify.session_ready_email(self.settings, to=session["client_email"], session_id=session_id,
                                             days=self.settings.audio_retention_days, language=session["language"])
        self.email_client(session_id, "session_ready", message)

    def send_due_emails(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Reminders about an undecided preview, and the follow-up after a session, when they are due (checked
        every hour). Only to clients who gave their email and did not stop these emails. Returns (session, kind)."""
        now = now or utc_now()
        sent = []
        retention = self.settings.audio_retention_days
        for session in self.db.sessions_for_emails(("awaiting_choice", "awaiting_session_payment")):
            if not session.get("preview_ready_at") or session.get("audio_expired_at"):
                continue
            age = now - datetime.fromisoformat(session["preview_ready_at"])
            due = [n for n, day in enumerate(self.settings.preview_reminder_days, 1) if age >= timedelta(days=day)]
            already = [int(kind.rsplit("_", 1)[1]) for kind in self.emails_sent(session["id"])
                       if kind.startswith("preview_reminder_")]
            days_left = retention - age.days
            if not due or max(already, default=0) >= due[-1] or days_left < 1:
                continue  # only the latest due reminder is sent: never a burst of old ones
            message = notify.preview_reminder_email(self.settings, to=session["client_email"],
                                                    session_id=session["id"], days_left=days_left,
                                                    session_price=self.price_gbp("session"),
                                                    language=session["language"])
            self.email_client(session["id"], f"preview_reminder_{due[-1]}", message)
            sent.append((session["id"], f"preview_reminder_{due[-1]}"))
        for session in self.db.sessions_for_emails(("ready",)):
            if not session.get("delivered_at") or "follow_up" in self.emails_sent(session["id"]) \
                    or self.db.get_followup(session["id"]) is not None:
                continue
            if now - datetime.fromisoformat(session["delivered_at"]) >= timedelta(days=self.settings.follow_up_days):
                message = notify.follow_up_email(self.settings, to=session["client_email"],
                                                 session_id=session["id"], days=self.settings.follow_up_days,
                                                 language=session["language"])
                self.email_client(session["id"], "follow_up", message)
                sent.append((session["id"], "follow_up"))
        return sent

    def follow_up(self, session: dict, *, severity: int, comments: str) -> None:
        """The client's answer to "how are you doing?" (the same 0-10 score as before their session)."""
        if session["status"] != "ready":
            raise Conflict("the follow-up is for a delivered session")
        if self.db.get_followup(session["id"]) is not None:
            raise Conflict("thank you, we already have your answer")
        self.db.add_followup(session_id=session["id"], severity=severity, comments=comments)

    def stop_emails(self, session: dict) -> None:
        self.db.set_session_fields(session["id"], emails_ok=0)

    def start_consultation(self, session: dict) -> None:
        """The client pressed Start. Their time starts with their first answer, so the minutes spent getting ready
        (or in silence) are not taken from the consultation they paid for."""
        if session["status"] != "consulting":
            raise Conflict("the consultation is not active")
        if not session.get("consultation_opened_at"):
            self.db.set_session_fields(session["id"], consultation_opened_at=stamp())

    def pause_consultation(self, session: dict) -> None:
        """The client has been silent for a while: the microphone stops, and so does their clock."""
        if session["status"] != "consulting":
            raise Conflict("the consultation is not active")
        if not session.get("paused_at"):
            self.db.set_session_fields(session["id"], paused_at=stamp())

    def resume_consultation(self, session: dict) -> None:
        """The client is back: the paused time is set aside, so it never counts towards their time."""
        if not session.get("paused_at"):
            return
        paused = 0
        if session.get("consultation_started_at"):  # time paused before the first answer was never counted
            since = max(datetime.fromisoformat(session["paused_at"]),
                        datetime.fromisoformat(session["consultation_started_at"]))
            paused = max(0, int((datetime.now(timezone.utc) - since).total_seconds()))
        self.db.set_session_fields(session["id"], paused_at=None,
                                   paused_seconds=(session.get("paused_seconds") or 0) + paused)

    def continue_consultation(self, session: dict) -> None:
        """A consultation that ended with too few answers to write a session from goes on, once, for a little longer."""
        if session["status"] != "consultation_incomplete" or not self.can_continue(session):
            raise Conflict("this consultation cannot be continued")
        self.db.transition(session["id"], ("consultation_incomplete",), "consulting", paused_at=None,
                           extra_minutes=(session.get("extra_minutes") or 0) + EXTRA_MINUTES)

    @staticmethod
    def can_continue(session: dict) -> bool:
        return session.get("coverage_note") != ENDED_EARLY and (session.get("extra_minutes") or 0) < EXTRA_MINUTES

    def enough_answers(self, answers: list[str]) -> bool:
        """Whether the client has said enough to write a session from: enough answers, or fewer, longer ones (a
        client who answers at length may give as much in five answers as another does in ten)."""
        return (len(answers) >= self.settings.consultation_min_answers
                or sum(len(answer.split()) for answer in answers) >= self.settings.consultation_min_words)

    def client_answers(self, session_id: str) -> list[str]:
        return [m["content"] for m in self.db.list_messages(session_id) if m["role"] == "patient"]

    def limit_seconds(self, session: dict) -> int:
        return (self.settings.consultation_minutes + (session.get("extra_minutes") or 0)) * 60

    def human_request_allowed(self, session: dict) -> bool:
        status = session["status"]
        if status in ("awaiting_choice", "awaiting_session_payment", "rejected", "consultation_incomplete"):
            return self.referral(session) != "doctor"
        if status == "screening_stopped":
            return session["screening_outcome"]["outcome"] == "human_only"
        if status == "consultation_stopped":
            return self.referral(session) == "hypnotherapist"
        return False

    def patient_view(self, session: dict) -> dict:
        session_id, status, language = session["id"], session["status"], session["language"]
        screening = session["screening_outcome"]
        stop = session.get("stop_detail") or {}
        view = {
            "status": status,
            "language": language,
            "screening_outcome": screening["outcome"],
            "screening_matched": screening["matched"],
            "stop_level": stop.get("level"),
            "referral": self.referral(session),
            "consultation_fee": self.consultation_fee(session_id),
            "human_request_allowed": self.human_request_allowed(session),
            "human_requested": status == "human_requested",
            "messages": [],
        }
        if status != "screening_stopped":
            view["messages"] = [{"role": m["role"], "content": m["content"]} for m in self.db.list_messages(session_id)]
        if status == "consulting":
            view["consultation_started"] = bool(session.get("consultation_opened_at")
                                                or session.get("consultation_started_at"))
            view["consultation_clock_started"] = bool(session.get("consultation_started_at"))
            view["consultation_paused"] = bool(session.get("paused_at"))
            view["consultation_elapsed_s"] = self.elapsed_seconds(session)
            view["consultation_minutes"] = self.limit_seconds(session) // 60
        if status == "consultation_incomplete":
            view["can_continue"] = self.can_continue(session)
            view["extra_minutes"] = EXTRA_MINUTES
            view["ended_early"] = session.get("coverage_note") == ENDED_EARLY
        for purpose, (waiting, _) in PAYMENT_STEPS.items():
            if status == waiting:
                view["payment"] = {"purpose": purpose, "amount": self.price_gbp(purpose), "currency": "GBP",
                                   "provider": self.settings.payment_provider,
                                   "saved_card": purpose == "session" and self.saved_card(session_id),
                                   "acknowledgement": self.acknowledgement(purpose, language),
                                   "test_mode": self.payments_test_mode}
        if status in AUDIO_STATUSES:
            view["preview_state"] = session.get("opening_status") or "writing"
        if session.get("client_email"):
            view["email"] = mask_email(session["client_email"])
            view["emails_ok"] = bool(session.get("emails_ok"))
        if status == "ready":
            view["follow_up_given"] = self.db.get_followup(session_id) is not None
        if screening["outcome"] == "crisis" or stop.get("level") == "crisis":
            view["crisis_resources"] = self.crisis.for_country(session["country"], language)
        intake = session.get("intake")
        if intake:
            view["summary"] = {
                "preferred_name": intake["preferred_name"],
                # in the client's language (the rest of the summary is in English, for the practice)
                "presenting_issue": intake.get("presenting_issue_for_client") or intake["presenting_issue"],
                "goal": intake.get("goal_for_client") or intake["goal_in_client_words"],
            }
            # The client chooses, unless the consultation's summary advised a qualified hypnotherapist instead.
            view["ai_not_suitable"] = session.get("opening_status") == "not_offered"
            view["ai_available"] = status == "awaiting_choice" and not view["ai_not_suitable"]
        if status == "ready":
            script = self.db.latest_script(session_id)
            view["session_script"] = patient_version(script["final_text"])
            view["feedback_given"] = self.db.get_feedback(session_id) is not None
            view["session_plan"] = [{"text": item["text"], "pause_ms": item["pause_ms"]}
                                    for item in delivery_plan(script["final_text"])]
            if background := self.session_background():
                view["session_background"] = {"volume": background["volume"]}
            view["session_reverb"] = self.session_reverb()
        return view

    def session_reverb(self) -> float:
        """How much reverb the session voice has, 0 (none) to 1, as the clinician set it."""
        return (self.db.get_setting("session_reverb") or {}).get("amount", DEFAULT_REVERB)

    def session_style(self, session: dict, text: str, position: int | None = None) -> str | None:
        """How a piece of this client's session is delivered (natural, slower, hypnotic). The position in the plan
        tells repeated lines apart ("That's it." in different parts); otherwise the first piece with that text."""
        if session["status"] != "ready":
            return None
        plan = delivery_plan(self.db.latest_script(session["id"])["final_text"])
        if position is not None and 0 <= position < len(plan) and plan[position]["text"] == text:
            return plan[position]["style"]
        return next((item["style"] for item in plan if item["text"] == text), None)

    # --- session background audio (chosen by the clinician from the audio files in data/voice) ---------------

    def audio_files(self) -> list[str]:
        folder = self.settings.data_dir / "voice"
        return sorted(p.name for p in folder.iterdir() if p.suffix.lower() in AUDIO_TYPES) if folder.is_dir() else []

    def session_background(self) -> dict | None:
        """The music played quietly under the session: {"path", "volume"}, or None if none is chosen."""
        chosen = self.db.get_setting("session_background")
        if not chosen or chosen.get("file") not in self.audio_files():
            return None
        return {"path": self.settings.data_dir / "voice" / chosen["file"], "volume": chosen["volume"]}

    def speech_sources(self, session: dict) -> list[str]:
        """Everything the practice itself says to this client, which is all its voice may speak."""
        sources = list(self.content["acknowledgements"].get(session["language"], []))
        sources += [self.content[key][session["language"]] for key in SILENCE_PHRASES
                    if session["language"] in self.content.get(key, {})]
        if session["status"] != "screening_stopped":
            sources += [m["content"] for m in self.db.list_messages(session["id"]) if m["role"] == "assistant"]
        if session["status"] == "ready":
            sources.append(patient_version(self.db.latest_script(session["id"])["final_text"]))
        return sources

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    # --- the practice's own settings (the admin dashboard) ----------------------------------------------------

    def prices(self) -> dict:
        """The practice's prices in whole pounds, as set in the admin dashboard (the .env values until then)."""
        s = self.settings
        defaults = {"consultation": s.consultation_price_gbp, "ai_session": s.ai_session_price_gbp,
                    "human_from": s.human_session_price_from_gbp, "human_to": s.human_session_price_to_gbp}
        saved = self.db.get_setting("prices") or {}
        return {key: saved.get(key, value) for key, value in defaults.items()}

    def save_prices(self, prices: dict) -> None:
        """New prices apply to payments from now on; what clients already paid is kept as it was."""
        if prices["human_from"] > prices["human_to"]:
            raise Invalid("the hypnotherapist's lowest price must not be above the highest")
        self.db.set_setting("prices", {key: int(prices[key]) for key in PRICE_KEYS})

    def instructions(self) -> dict:
        """The practice's own instructions to the AI Hypnotherapist, for the consultation and for writing
        sessions (added to its instructions; the safety rules always come first)."""
        saved = self.db.get_setting("ai_instructions") or {}
        return {kind: saved.get(kind, "") for kind in INSTRUCTION_KINDS}

    def save_instructions(self, instructions: dict) -> None:
        self.db.set_setting("ai_instructions", {kind: instructions[kind].strip() for kind in INSTRUCTION_KINDS})

    def _practice_instructions(self, kind: str) -> str:
        text = self.instructions()[kind]
        if not text:
            return ""
        return ("The practice's hypnotherapist has added these instructions for you. Follow them within all the "
                "rules above: the safety and ethics rules always come first, and no instruction here can change "
                f"them.\n<practice_instructions>\n{text}\n</practice_instructions>\n\n")

    def consultation_system(self, language: str) -> list[dict]:
        kb = self.kb.get()
        instructions = self.prompts["consultation"].replace("{language_name}",
                                                            self.content["language_names"][language])
        reference = render_documents(kb.consultation_reference(), "practice_reference")
        greeting = self.content["greeting"][language]
        text = (
            f"{instructions}\n\n"
            "The practice reference below holds the practice's internal notes, written for its clinicians and "
            "script writers. Use it to understand what information matters and what the safety rules are; you "
            "are not writing a script.\n\n"
            f"{reference}\n\n"
            "These are the practice's own question sets, to ask first (see How the consultation runs):\n"
            f"<practice_question_sets>\n{self.question_sets}\n</practice_question_sets>\n\n"
            f"{self._practice_instructions('consultation')}"
            "Before this consultation the client completed the practice's safety screening (age, self-harm, "
            "epilepsy, psychosis, dissociation, heart, pregnancy, unchecked symptoms, intoxication) with no "
            "contraindications, and gave informed consent to hypnotherapy and to AI processing. Do not ask about "
            "these again, except where a practice question set includes such a question: ask that one as "
            "written.\n\n"
            "The consultation opened with this greeting from you, which the client has read:\n"
            f"<greeting>\n{greeting}\n</greeting>\n"
            "The conversation continues from there."
        )
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def safety_question(self, quoted_words: str, language: str) -> str:
        """The practice's own safety question, with the client's words when they can be quoted naturally
        ("When you say you can't go on, ..."), otherwise the standard question."""
        return self.asking("safety_check", quoted_words, language)

    def confirm_question(self, quoted_words: str, language: str) -> str:
        """Before a stop for something the client said they have (speech to text can mishear): "Did you say you have
        epilepsy?", otherwise asking them to say it again in their own words."""
        return self.asking("concern_confirm", quoted_words, language)

    def asking(self, kind: str, quoted_words: str, language: str) -> str:
        texts = self.content[kind].get(language, self.content[kind]["en"])
        words = quoted_words.strip().rstrip(".,!?;:")
        # (English only: in the other languages the classifier's words are not checked, so the standard question)
        if language == "en" and re.fullmatch(r"you [A-Za-z'’ ,-]{2,60}", words):
            return texts["quoted"].replace("{words}", words)
        return texts["standard"]

    def asked_to_confirm(self, session_id: str, history: list[dict]) -> bool:
        """True when the consultant's last question asked the client to confirm what was heard: this is the answer."""
        last_answer = next((m["id"] for m in reversed(history) if m["role"] == "patient"), None)
        events = self.db.list_risk_events(session_id)
        return bool(events) and events[-1]["level"] == "confirm" and events[-1]["message_id"] == last_answer

    def elapsed_seconds(self, session: dict) -> int:
        """Seconds since the client pressed Start (0 before that)."""
        if not session.get("consultation_started_at"):
            return 0
        now = datetime.now(timezone.utc)
        started = datetime.fromisoformat(session["consultation_started_at"])
        paused = session.get("paused_seconds") or 0
        if session.get("paused_at"):
            paused += max(0, (now - max(datetime.fromisoformat(session["paused_at"]), started)).total_seconds())
        return max(0, int((now - started).total_seconds() - paused))

    def expire_consultation(self, session: dict) -> str | None:
        """Closes a consultation at its time limit, even if the client sends nothing more (or left it paused for a
        day). Returns where it went: "extracting" (its summary is written next) or "consultation_incomplete" (too few
        answers to write a session from: nothing more is sent to the AI); None if it is still open."""
        if session.get("status") != "consulting":
            return None
        now = datetime.now(timezone.utc)
        abandoned = bool(session.get("paused_at")) and now - datetime.fromisoformat(session["paused_at"]) > PAUSE_ABANDONED
        if not abandoned and (not session.get("consultation_started_at") or session.get("paused_at")
                              or self.elapsed_seconds(session) < self.limit_seconds(session)):
            return None
        answers = self.client_answers(session["id"])
        to = "extracting" if self.enough_answers(answers) else "consultation_incomplete"
        note = ("Closed by the server: the client left the consultation paused." if abandoned
                else "Stopped by the server at the consultation time limit.")
        if not self.db.transition(session["id"], ("consulting",), to, paused_at=None, coverage_note=note):
            return None
        log.info("Session %s: consultation closed (%s), %d answers: %s", session["id"][:6],
                 "left paused" if abandoned else "time limit", len(answers), to)
        return to

    def end_consultation(self, session: dict) -> str:
        """The client stops the consultation before its time is up, for good (they were told so, and that the
        consultation fee is not refunded). A session and its preview are only ever written from a full consultation,
        so nothing is written; the hypnotherapist is still offered."""
        if session["status"] != "consulting":
            raise Conflict("the consultation is not active")
        if not self.db.transition(session["id"], ("consulting",), "consultation_incomplete", paused_at=None,
                                  coverage_note=ENDED_EARLY):
            raise Conflict("the session changed; please reload")
        log.info("Session %s: consultation ended early by the client", session["id"][:6])
        return "consultation_incomplete"

    def expire_overdue_consultations(self) -> list[str]:
        """Closes every consultation past its time limit, also when the client left without closing it (the app
        closed, the phone switched off). Returns the sessions whose summary is to be written now."""
        return [session["id"] for session in self.db.sessions_in(("consulting",))
                if self.expire_consultation(session) == "extracting"]

    def time_note(self, elapsed_s: int, patient_count: int, limit_s: int | None = None, enough: bool = True) -> str:
        minutes = elapsed_s // 60
        target = (limit_s or self.settings.consultation_minutes * 60) // 60
        note = f"Time used so far: {minutes} of about {target} minutes."
        if not enough:  # a session can only be written from enough of the client's own answers
            note += (f" The client has given {patient_count} answers, not yet enough to write their session: keep the "
                     "consultation going and do not close it yet.")
        elif minutes >= target - 1 or patient_count >= self.settings.soft_turn_limit:
            note += " " + self.content["wrap_up_instruction"]
        return note

    async def consultation_turn(self, session_id: str, text: str,
                                voice_features: dict | None = None) -> AsyncIterator[tuple[str, dict]]:
        """One client answer -> reply. The safety check and the consultant run at the same time; the reply is
        only released once the safety check has passed. Yields (event, payload) pairs."""
        session = self.db.get_session(session_id)
        if session is None or session["status"] != "consulting":
            yield "error", {"code": "not_consulting"}
            return
        if session.get("paused_at"):  # an answer means the client is back
            self.resume_consultation(session)
        if not session.get("consultation_started_at"):  # their time starts with their first answer
            self.db.set_session_fields(session_id, consultation_started_at=stamp())
        session = self.db.get_session(session_id)
        language = session["language"]
        history = self.db.list_messages(session_id)
        patient_count = sum(1 for m in history if m["role"] == "patient") + 1
        elapsed = self.elapsed_seconds(session)
        enough = self.enough_answers([m["content"] for m in history if m["role"] == "patient"] + [text])
        voice = None
        if voice_features:
            earlier = [m["voice"]["features"] for m in history if m["role"] == "patient" and m.get("voice")]
            voice = {"features": voice_features, "summary": describe_voice(voice_features, earlier)}
        hard_stop = (patient_count >= self.settings.hard_turn_limit
                     or elapsed >= self.limit_seconds(session))

        reply_task = None
        if not hard_stop:
            try:
                system = self.consultation_system(language)
            except KnowledgeBaseError as exc:
                log.error("Session %s: knowledge base unavailable: %s", session_id, exc)
                yield "error", {"code": "ai_unavailable"}
                return
            messages = to_api_messages(history + [{"role": "patient", "content": text, "voice": voice}])
            messages.append({"role": "system",
                             "content": self.time_note(elapsed, patient_count, self.limit_seconds(session), enough)})
            reply_task = asyncio.create_task(self.gateway.parse(
                system=system, messages=messages, output_model=ConsultantTurn,
                effort=self.settings.consultation_effort, max_tokens=16000,
            ))

        try:
            decision = await assess(
                self.gateway, self.keywords, system_prompt=self.prompts["risk_classifier"], history=history,
                patient_message=text, language=language, effort=self.settings.risk_effort,
            )
        except BaseException:
            if reply_task:
                discard(reply_task)
            raise
        if decision.llm_result:
            self.record(session_id, "risk_check", decision.llm_result)
        if decision.level != "none" and reply_task:
            discard(reply_task)
        if decision.level == "unverified":
            log.error("Session %s: safety check unavailable: %s", session_id, decision.rationale)
            yield "error", {"code": decision.error_code or "safety_unavailable"}
            return
        if decision.level == "check":  # ambiguous words: ask the practice's safety question before going on
            message_id = self.db.add_message(session_id, "patient", text, voice)
            self.db.add_risk_event(
                session_id=session_id, message_id=message_id, level="check", categories=decision.categories,
                evidence=decision.evidence, rationale=decision.rationale, keyword_hits=decision.keyword_hits,
                source=decision.source,
            )
            question = self.safety_question(decision.quoted_words, language)
            self.db.add_message(session_id, "assistant", question)
            yield "delta", {"text": question}
            yield "done", {"consultation_complete": False, "delivery": "gentle"}
            return
        if decision.level == "concern" and not self.asked_to_confirm(session_id, history):
            # Said they have something that stops the consultation: asked once more first, as speech to text can
            # mishear. Their answer decides (the safety check judges it with this question in view).
            message_id = self.db.add_message(session_id, "patient", text, voice)
            self.db.add_risk_event(
                session_id=session_id, message_id=message_id, level="confirm", categories=decision.categories,
                evidence=decision.evidence, rationale=decision.rationale, keyword_hits=decision.keyword_hits,
                source=decision.source,
            )
            question = self.confirm_question(decision.quoted_words, language)
            self.db.add_message(session_id, "assistant", question)
            yield "delta", {"text": question}
            yield "done", {"consultation_complete": False, "delivery": "gentle"}
            return
        if decision.level in ("crisis", "concern"):
            message_id = self.db.add_message(session_id, "patient", text, voice)
            self.db.add_risk_event(
                session_id=session_id, message_id=message_id, level=decision.level,
                categories=decision.categories, evidence=decision.evidence, rationale=decision.rationale,
                keyword_hits=decision.keyword_hits, source=decision.source,
            )
            self.db.transition(session_id, ("consulting",), "consultation_stopped", stop_detail={
                "level": decision.level, "categories": decision.categories, "source": decision.source,
            })
            payload = {"level": decision.level}
            if decision.level == "crisis":
                payload["crisis_resources"] = self.crisis.for_country(session["country"], language)
            yield "risk", payload
            return

        if hard_stop:
            closing = self.content["closing_at_limit" if enough else "closing_too_few_answers"][language]
            self.db.add_message(session_id, "patient", text, voice)
            self.db.add_message(session_id, "assistant", closing)
            self.db.transition(session_id, ("consulting",), "extracting" if enough else "consultation_incomplete",
                               coverage_note="Stopped by the server at the maximum consultation length.")
            yield "delta", {"text": closing}
            yield "done", {"consultation_complete": True, "delivery": "warm"}
            return

        try:
            turn, result = await reply_task
        except LLMRefusal:
            self.db.add_message(session_id, "patient", text, voice)
            self.db.transition(session_id, ("consulting",), "consultation_stopped",
                               stop_detail={"level": "handoff", "categories": [], "source": "model_refusal"})
            yield "handoff", {}
            return
        except (LLMUnavailable, KnowledgeBaseError) as exc:
            log.error("Session %s: consultation reply failed: %s", session_id, exc)
            yield "error", {"code": "usage_limit" if isinstance(exc, LLMUsageLimit) else "ai_unavailable"}
            return
        self.record(session_id, "consultation", result)

        reply = no_em_dashes(turn.reply.strip())
        if not reply:
            log.error("Session %s: empty consultation reply", session_id)
            yield "error", {"code": "ai_unavailable"}
            return

        message_id = self.db.add_message(session_id, "patient", text, voice)
        self.db.add_answer_analysis(session_id=session_id, message_id=message_id,
                                    analysis={**turn.analysis.model_dump(), "delivery": turn.delivery})
        self.db.add_message(session_id, "assistant", reply)
        yield "delta", {"text": reply}
        if not turn.consultation_complete or not enough:  # never closed before the answers a session needs
            yield "done", {"consultation_complete": False, "delivery": turn.delivery}
            return
        self.db.transition(session_id, ("consulting",), "extracting", coverage_note=turn.coverage_note.strip())
        yield "done", {"consultation_complete": True, "delivery": turn.delivery}

    def interrupted_work(self) -> list[tuple[str, object]]:
        """(session id, the step to run) for every step a restart interrupted."""
        work = [(s["id"], self.run_extraction) for s in self.db.sessions_in(("extracting",))]
        work += [(s["id"], self.run_generation) for s in self.db.sessions_in(("generating",))]
        work += [(s["id"], self.run_opening) for s in self.db.sessions_in(("awaiting_choice", "awaiting_session_payment"))
                 if s.get("opening_status") == "writing"]
        return work

    def background_to_resume(self, session: dict):
        """The background step a session is waiting for when none is running (e.g. the client disconnected)."""
        if session["id"] in self._inflight:
            return None
        if session["status"] in ("awaiting_choice", "awaiting_session_payment") \
                and session.get("opening_status") == "writing":
            return self.run_opening  # the preview's script was being written when the app stopped
        return {"extracting": self.run_extraction, "generating": self.run_generation}.get(session["status"])

    async def run_extraction(self, session_id: str) -> None:
        if session_id in self._inflight:
            return
        self._inflight.add(session_id)
        try:
            await self._extract(session_id)
        finally:
            self._inflight.discard(session_id)

    async def _extract(self, session_id: str) -> None:
        session = self.db.get_session(session_id)
        if session is None or session["status"] != "extracting":
            return
        try:
            intake, result = await extract_intake(
                self.gateway, system_prompt=self.prompts["intake_extraction"].replace(
                    "{language_name}", self.content["language_names"].get(session["language"], "British English")),
                messages=self.db.list_messages(session_id), coverage_note=session.get("coverage_note"),
                effort=self.settings.extraction_effort,
            )
        except (LLMUnavailable, LLMRefusal) as exc:
            log.error("Session %s: intake extraction failed: %s", session_id, exc)
            self.db.transition(session_id, ("extracting",), "extraction_failed")
            return
        self.record(session_id, "intake_extraction", result)
        # No session is written or offered when the summary finds a safety reason (the client is sent to a doctor), or
        # when the client's issue is one the practice's hypnotherapist works with himself (see HypnotherapistIssue).
        # The consultation fee is not refunded: the client confirmed their safety answers before paying.
        offered = (intake.suitability_for_self_guided_session.suitable and intake.hypnotherapist_issue == "none"
                   ) or self.settings.offer_ai_when_unsuitable
        if self.db.transition(session_id, ("extracting",), "awaiting_choice",
                              intake=without_em_dashes(intake.model_dump()),
                              intake_meta={"model": result.model,
                                           "prompt_version": self.prompt_versions["intake_extraction"]},
                              opening_status="writing" if offered else "not_offered"):
            if offered:
                await self._write_opening(session_id)  # the free preview: only the opening is written for now
            else:
                await asyncio.to_thread(self.send_next_step, session_id)

    def retry_extraction(self, session_id: str) -> None:
        if not self.db.transition(session_id, ("extraction_failed",), "extracting"):
            raise Conflict("extraction can only be retried after it has failed")

    def choose_ai_session(self, session: dict, baseline_severity: int) -> None:
        """The client's own choice (the practice's rule): on to paying for the full session."""
        if session.get("opening_status") == "not_offered":
            raise Conflict("a session with a qualified hypnotherapist is advised for this consultation")
        if not self.db.transition(session["id"], ("awaiting_choice",), "awaiting_session_payment",
                                  baseline_severity=baseline_severity):
            raise Conflict("a session can only be chosen after the consultation summary is ready")

    def back_to_choice(self, session: dict) -> None:
        """The client leaves the payment page without paying, to choose again."""
        if not self.db.transition(session["id"], ("awaiting_session_payment",), "awaiting_choice"):
            raise Conflict("there is no payment to leave")

    async def run_opening(self, session_id: str) -> None:
        if session_id in self._inflight:
            return
        self._inflight.add(session_id)
        try:
            await self._write_opening(session_id)
        finally:
            self._inflight.discard(session_id)

    def retry_preview(self, session: dict) -> None:
        """The client asks for their preview again after its opening could not be written."""
        if session["status"] not in ("awaiting_choice", "awaiting_session_payment") \
                or session.get("opening_status") != "failed":
            raise Conflict("the preview can only be prepared again after it failed")
        self.db.set_session_fields(session["id"], opening_status="writing")

    def _script_inputs(self, session: dict) -> dict:
        return {
            "system_prompt": self.prompts["script_writer"].replace(
                "{language_name}", self.content["language_names"][session["language"]])
            + ("\n\n" + self._practice_instructions("script").strip() if self.instructions()["script"] else ""),
            "intake": session["intake"], "lessons": self.db.approved_lessons(),
            "screening": render_screening(session["screening_version"], self.screening.questions_for("en"),
                                          session["screening_answers"]),
            "effort": self.settings.script_effort,
        }

    async def _write_opening(self, session_id: str) -> bool:
        """Writes the session's opening (title, pre-talk, induction), from which the preview is made. True when an
        opening that passed its checks is ready."""
        session = self.db.get_session(session_id)
        if session is None or session.get("opening_status") != "writing":
            return False
        fix, text, issues = None, "", []
        for attempt in (1, 2):
            try:
                text, _, result = await write_script(self.gateway, self.kb.get(), **self._script_inputs(session),
                                                     fix=fix, part="opening")
            except (LLMUnavailable, LLMRefusal, KnowledgeBaseError) as exc:
                log.error("Session %s: the opening could not be written: %s", session_id, exc)
                self.db.set_session_fields(session_id, opening_status="failed")
                return False
            self.record(session_id, "script_opening", result)
            text = no_em_dashes(text)
            issues = validate_opening(text, stop_reason=result.stop_reason, language=session["language"])
            fix = [issue for issue in issues if not issue.startswith("The opening is too short")]
            if not fix:
                break
            log.warning("Session %s: opening draft %d failed checks (%s)", session_id, attempt, "; ".join(fix))
        self.db.add_opening(session_id=session_id, model=result.model,
                            prompt_version=self.prompt_versions["script_writer"], text=text,
                            validation_issues=issues, usage=usage_totals(result.usage))
        ok = not fix
        self.db.set_session_fields(session_id, opening_status="ready" if ok else "failed")
        if ok:
            self._delivered(session_id)  # its preview can be made now
        return ok

    async def run_generation(self, session_id: str) -> None:
        if session_id in self._inflight:
            return
        self._inflight.add(session_id)
        try:
            await self._generate(session_id)
        finally:
            self._inflight.discard(session_id)

    async def _generate(self, session_id: str) -> None:
        """After the session is paid for: the rest of the script, continuing from the opening the preview was made
        from (the opening is written first if it is missing)."""
        session = self.db.get_session(session_id)
        if session is None or session["status"] != "generating":
            return
        if session.get("opening_status") != "ready" or self.db.latest_opening(session_id) is None:
            self.db.set_session_fields(session_id, opening_status="writing")
            if not await self._write_opening(session_id):
                self.db.transition(session_id, ("generating",), "generation_failed")
                return
            session = self.db.get_session(session_id)
        self._delivered(session_id)  # the rest of the opening can be spoken while the rest is written
        opening = self.db.latest_opening(session_id)["text"]
        inputs = self._script_inputs(session)
        fix = None
        for attempt in (1, 2):  # a draft that fails a check is written once more, with the problems named
            try:
                rest, sources, result = await write_script(self.gateway, self.kb.get(), **inputs, fix=fix,
                                                           part="rest", opening=opening)
            except (LLMUnavailable, LLMRefusal, KnowledgeBaseError) as exc:
                log.error("Session %s: script generation failed: %s", session_id, exc)
                self.db.transition(session_id, ("generating",), "generation_failed")
                return
            self.record(session_id, "script", result)
            script = assemble_script(opening, no_em_dashes(rest))
            issues = validate_script(script, category=session["intake"]["presenting_issue_category"],
                                     stop_reason=result.stop_reason, language=session["language"])
            if not must_fix(issues):
                break
            log.warning("Session %s: draft %d failed checks (%s)", session_id, attempt, "; ".join(must_fix(issues)))
            fix = must_fix(issues)
        script_id = self.db.add_script(
            session_id=session_id, model=result.model, prompt_version=self.prompt_versions["script_writer"],
            knowledge_sources=sources, lessons_used=[lesson["id"] for lesson in inputs["lessons"]], draft=script,
            validation_issues=issues, usage=usage_totals(result.usage),
        )
        if self.settings.require_clinician_review:
            self.db.transition(session_id, ("generating",), "pending_review")
        elif must_fix(issues):  # never delivered without its safety parts: the client can ask for it again
            self.db.transition(session_id, ("generating",), "generation_failed")
        else:
            self.db.mark_script_auto_approved(script_id)
            if self.db.transition(session_id, ("generating",), "ready"):
                self.db.set_session_fields(session_id, delivered_at=utc_now().isoformat(timespec="seconds"))
            self._delivered(session_id)

    def _delivered(self, session_id: str) -> None:
        """The opening or the whole script is ready: its audio can now be made (see session_audio)."""
        if self.on_delivered is not None:
            self.on_delivered(session_id)

    def audio_job(self, session_id: str) -> dict | None:
        """What the session's audio is made from now, or None when there is nothing to make:
        - "preview": before the session is paid for, only the first five minutes of the opening;
        - "opening": paid, the rest still being written, so the whole opening is spoken meanwhile;
        - "full": the whole session, mixed into the file the client downloads."""
        session = self.db.get_session(session_id)
        if session is None or session.get("audio_expired_at") or session["status"] not in AUDIO_STATUSES:
            return None
        background = self.session_background()
        voice = self.db.get_setting("voice") or {}
        job = {"language": session["language"], "session_rate": voice.get("session_rate", 0.8),
               "reverb": self.session_reverb(),
               "music": str(background["path"]) if background else None,
               "music_gain": background["volume"] if background else 0.0}
        if session["status"] == "ready":
            script = self.db.latest_script(session_id)
            return {**job, "phase": "full", "script": script["final_text"], "key": f"script-{script['id']}"}
        opening = self.db.latest_opening(session_id)
        if session.get("opening_status") != "ready" or opening is None:
            return None
        phase = "opening" if session["status"] == "generating" else "preview"
        return {**job, "phase": phase, "script": opening["text"], "key": f"opening-{opening['id']}"}

    def audio_expired(self, session_id: str) -> None:
        """The session's audio has been deleted (kept for the retention period only). A session still waiting for
        its payment expires with it."""
        self.db.set_session_fields(session_id, audio_expired_at=datetime.now(timezone.utc).isoformat(
            timespec="seconds"))
        self.db.transition(session_id, ("awaiting_choice", "awaiting_session_payment"), "expired")

    def retry_generation(self, session_id: str) -> None:
        """The client asks for their session again after it could not be written."""
        if not self.db.transition(session_id, ("generation_failed",), "generating"):
            raise Conflict("a session can only be written again after it could not be written")

    def request_human(self, session: dict, *, name: str, email: str, phone: str, description: str,
                      baseline_severity: int | None) -> None:
        if not self.human_request_allowed(session):
            raise Conflict("a hypnotherapist request is not available at this stage")
        origin = session["status"]
        fields = {} if baseline_severity is None else {"baseline_severity": baseline_severity}
        if not self.db.transition(session["id"], (origin,), "human_requested", **fields):
            raise Conflict("the session changed; please reload")
        self.db.add_human_request(session_id=session["id"], name=name, email=email, phone=phone,
                                  description=description, origin=origin)

    def notify_human_request(self, session_id: str) -> str:
        """Emails the practice about a hypnotherapist request (saved to data/outbox if the email cannot be sent,
        so no request is ever lost). Returns "sent" or "saved"."""
        request = self.db.get_human_request(session_id)
        session = self.db.get_session(session_id) or {}
        intake = session.get("intake")
        summary = {"presenting_issue": intake["presenting_issue"], "goal": intake["goal_in_client_words"]} \
            if intake else None
        messages = self.db.list_messages(session_id)
        pdf, questions = None, 0
        if any(m["role"] == "patient" for m in messages):  # they had a consultation: every question and answer
            messages = [{**m, "content": no_em_dashes(m["content"]) if m["role"] == "assistant" else m["content"]}
                        for m in messages]
            questions = sum(1 for m, after in zip(messages, messages[1:])
                            if m["role"] == "assistant" and after["role"] == "patient")
            pdf = consultation_pdf(
                practice=self.settings.practice_name, session_ref=session_id[:6],
                client={"name": request["name"], "email": request["email"], "phone": request["phone"]},
                started_at=session.get("consultation_started_at"), messages=messages)
        message = notify.human_request_email(
            self.settings, session_id=session_id, name=request["name"], email=request["email"],
            phone=request["phone"], description=request["description"], summary=summary, transcript_pdf=pdf,
            questions=questions, language="English" if session.get("language", "en") == "en"
            else self.content["language_names"].get(session["language"], session["language"]))
        outbox = self.settings.data_dir / "outbox"
        try:
            return notify.send(self.settings, message, outbox)
        except Exception:  # noqa: BLE001 - any mail failure: keep the request in the outbox instead
            log.exception("Session %s: the hypnotherapist request email could not be sent", session_id[:6])
            return notify.send(replace(self.settings, smtp_host=""), message, outbox)

    def add_feedback(self, session: dict, *, rating: int | None, severity_after: int | None, comments: str) -> None:
        if session["status"] != "ready":
            raise Conflict("feedback can be given once the session is ready")
        if self.db.get_feedback(session["id"]) is not None:
            raise Conflict("feedback has already been given")
        self.db.add_feedback(session_id=session["id"], rating=rating, severity_after=severity_after,
                             comments=comments)

    # --- clinician --------------------------------------------------------------

    def clinician_view(self, session_id: str) -> dict | None:
        session = self.db.get_session(session_id)
        if session is None:
            return None
        usage = self.db.usage_by_session(session_id)
        return {
            "session": session,
            "messages": self.db.list_messages(session_id),
            "answer_analyses": self.db.answer_analyses(session_id),
            "risk_events": self.db.list_risk_events(session_id),
            "script": self.db.latest_script(session_id),
            "human_request": self.db.get_human_request(session_id),
            "payments": self.db.list_payments(session_id),
            "emails": self.db.list_emails(session_id),
            "followup": self.db.get_followup(session_id),
            "opening": self.db.latest_opening(session_id),
            "feedback": self.db.get_feedback(session_id),
            "usage": usage,
            "estimated_cost_usd": estimate_cost_usd(usage),
        }

    def review(self, session_id: str, *, decision: str, final_text: str | None, notes: str) -> None:
        script = self.db.latest_script(session_id)
        if script is None:
            raise Conflict("there is no script to review")
        if decision == "approve":
            if not self.db.transition(session_id, ("pending_review",), "ready"):
                raise Conflict("only a script awaiting review can be approved")
            text = (final_text or "").strip() or script["draft"]
            self.db.review_script(script["id"], decision="approved", final_text=text, notes=notes)
            self.db.set_session_fields(session_id, delivered_at=utc_now().isoformat(timespec="seconds"))
            self._delivered(session_id)
        else:
            if not self.db.transition(session_id, ("pending_review",), "rejected"):
                raise Conflict("only a script awaiting review can be rejected")
            self.db.review_script(script["id"], decision="rejected", final_text=None, notes=notes)

    def regenerate(self, session_id: str) -> None:
        if not self.db.transition(session_id, ("generation_failed", "pending_review", "rejected"), "generating"):
            raise Conflict("a new draft can only be generated after a failure, or for a script under review")

    async def propose_lessons(self) -> list[int]:
        reviews = self.db.reviewed_scripts(limit=20)
        if not reviews:
            return []
        proposals, result = await propose_lessons(
            self.gateway, system_prompt=self.prompts["lessons"], reviews=reviews,
            approved=self.db.approved_lessons(), effort="high",
        )
        self.record(None, "lessons", result)
        return [
            self.db.add_lesson(lesson=no_em_dashes(p.lesson), rationale=no_em_dashes(p.rationale),
                               evidence_ids=p.evidence_script_ids)
            for p in proposals
        ]

    def knowledge_status(self) -> dict:
        kb = self.kb.get()
        return {
            "root": str(kb.root),
            "library_sections": len(kb.chunks),
            "library_files": len({c.source_path for c in kb.chunks}),
            "blocked_files": kb.blocked_files,
            "dropped_sections": kb.dropped_sections,
        }
