"""HTTP layer. Run with:  uvicorn app.main:create_app --factory"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path as FsPath
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders

from . import notify
from .admin_auth import COOKIE, REQUEST_HEADER, SESSION_HOURS, AdminAuth, SignInRefused
from .books import MAX_UPLOAD_BYTES
from .books import Conflict as BookConflict
from .claude_code import ClaudeCodeGateway, find_claude_code
from .config import STATIC_DIR, Settings, load_settings
from .knowledge import KnowledgeBaseError
from .llm import ClaudeGateway, LLMRefusal, LLMUnavailable
from .screening import ScreeningError
from .payments import PaymentUnavailable
from .service import AUDIO_STATUSES, AUDIO_TYPES, SILENCE_PHRASES, Conflict, Invalid, Service
from .session_audio import SessionAudio
from .session_delivery import SLOWER, fades_for, rate_for
from .speech import LANGUAGES as SPEECH_LANGUAGES
from .speech import MAX_TEXT_CHARS, SAMPLE_RATE, PracticeVoice, pocket_tts_installed, sentences, speakable, spoken_form
from . import transcribe as transcribe_module

log = logging.getLogger("app")

SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{20,64}$")]
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class StartSessionIn(BaseModel):
    country: str = Field(max_length=16)
    language: str = Field(max_length=8)
    answers: dict[str, StrictBool]


class VoiceFeaturesIn(BaseModel):
    """Numbers measured from the client's voice in their browser. Only numbers are accepted; the wording the
    consultant sees is produced on the server (app/voice_cues.py)."""
    duration_s: float = Field(ge=0, le=600)
    speech_ratio: float = Field(ge=0, le=1)
    loudness_db: float = Field(ge=-120, le=0)
    pitch_hz: float = Field(ge=0, le=800)
    pitch_variation: float = Field(ge=0, le=5)
    pause_count: int = Field(ge=0, le=500)
    longest_pause_s: float = Field(ge=0, le=600)
    words_per_minute: float = Field(ge=0, le=600)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: VoiceFeaturesIn | None = None


class AISessionIn(BaseModel):
    baseline_severity: int = Field(ge=0, le=10)


class HumanRequestIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)  # full name
    email: str = Field(max_length=200, pattern=EMAIL_PATTERN)
    phone: str = Field(min_length=5, max_length=40, pattern=r"^[0-9+()\-\s]+$")  # mobile number
    description: str = Field(min_length=1, max_length=2000)  # a short description of the problem
    baseline_severity: int | None = Field(default=None, ge=0, le=10)


class PaymentIn(BaseModel):
    purpose: Literal["consultation", "session"]
    save_card: bool = False
    acknowledged: bool = False  # the client confirmed the words about their right to cancel
    email: str | None = Field(default=None, max_length=200, pattern=EMAIL_PATTERN)  # for the consultation payment
    emails_ok: bool = True  # reminders and the follow-up (the client can say no here, or stop them later)
    amount: int | None = Field(default=None, ge=0, le=100_000)  # the price the client saw (refused if it changed)


class ConfirmPaymentIn(BaseModel):
    checkout_id: str = Field(min_length=3, max_length=200, pattern=r"^cs_[A-Za-z0-9_]+$")


class SignInIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    robot: dict | None = None  # the solved robot check


class PricesIn(BaseModel):
    """The practice's prices in whole pounds."""
    consultation: int = Field(ge=1, le=10_000)
    ai_session: int = Field(ge=1, le=10_000)
    human_from: int = Field(ge=1, le=100_000)
    human_to: int = Field(ge=1, le=100_000)


class InstructionsIn(BaseModel):
    """The practice's own instructions to the AI Hypnotherapist."""
    consultation: str = Field(default="", max_length=6000)
    script: str = Field(default="", max_length=6000)


class FollowUpIn(BaseModel):
    severity: int = Field(ge=0, le=10)
    comments: str = Field(default="", max_length=2000)


class FeedbackIn(BaseModel):
    comments: str = Field(min_length=1, max_length=2000)
    rating: int | None = Field(default=None, ge=1, le=5)  # optional: the app asks only for comments
    severity_after: int | None = Field(default=None, ge=0, le=10)


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    final_text: str | None = Field(default=None, max_length=200_000)
    notes: str = Field(default="", max_length=5000)


class LessonDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]


class SpeechIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    kind: Literal["conversation", "session"] = "conversation"
    position: int | None = Field(default=None, ge=0, le=100_000)  # a session piece's place in the delivery plan


class PreviewSpeechIn(SpeechIn):
    rate: float | None = Field(default=None, ge=0.3, le=2.0)  # try a session speed before saving it


class BackgroundIn(BaseModel):
    """The music under the session: a file from data/voice (None: no music) and its volume (0-2, 1 = as recorded)."""
    file: str | None = Field(default=None, max_length=200)
    volume: float = Field(ge=0, le=2)


DEFAULT_BACKGROUND_VOLUME = 1.43  # 0.9 raised by 4 dB, as the practice asked


class ReverbIn(BaseModel):
    """How much reverb the session voice has: 0 (none) to 1."""
    amount: float = Field(ge=0, le=1)


class VoiceSettingsIn(BaseModel):
    """The single practice voice. Values follow the Web Speech API ranges the test app uses."""
    voice_name: str = Field(default="", max_length=200)
    rate: float = Field(ge=0.3, le=2.0)
    pitch: float = Field(ge=0.1, le=2.0)
    session_rate: float = Field(ge=0.3, le=2.0)


# Until the clinician saves a practice voice: the best natural male voice on the device, calm and unhurried.
DEFAULT_VOICE = {"voice_name": "", "rate": 0.9, "pitch": 0.9, "session_rate": 0.8}


ADMIN_PAGES = {"/admin", "/sign-in", "/clinician"}


class SecurityHeaders:
    """Pure ASGI middleware, so streamed (SSE) responses are never buffered. embed_origins: websites that may show
    the client page inside theirs (the practice's website); nothing else can, and the admin pages never."""

    def __init__(self, app, embed_origins: tuple[str, ...] = ()):
        self.app = app
        self.client_csp = CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self' " + " ".join(embed_origins)) \
            if embed_origins else CSP

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith("/api/")
        is_page = scope["path"] == "/" or scope["path"].startswith("/static/")
        is_admin = scope["path"] in ADMIN_PAGES

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                if scope["path"] == "/" and self.client_csp != CSP:  # the client page, shown on the website
                    headers["Content-Security-Policy"] = self.client_csp
                else:
                    headers["X-Frame-Options"] = "DENY"
                    headers["Content-Security-Policy"] = CSP
                if is_admin:  # never kept by the browser or a proxy, and never listed by search engines
                    headers["Cache-Control"] = "no-store"
                    headers["X-Robots-Tag"] = "noindex, nofollow"
                elif is_api and "cache-control" not in headers:  # a route may allow caching (the session music)
                    headers["Cache-Control"] = "no-store"
                elif is_page and "cache-control" not in headers:  # always the latest version after an update
                    headers["Cache-Control"] = "no-cache"  # (checked each time; unchanged files are not re-sent)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestLog:
    """Logs every API request with its result and how long it took (for streams: until the last byte), so problems
    such as slow replies can be traced afterwards. Session ids are shortened."""

    SESSION = re.compile(r"(/api/sessions/[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 0

        async def watch(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            log.info("%s %s -> %s in %.0f ms", scope["method"], self.SESSION.sub(r"\1...", scope["path"]), status,
                     (time.monotonic() - started) * 1000)


class LocalOnly:
    """While the subscription-based test engine is active, serve nobody but this computer."""

    LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope.get("client") or ("",))[0] not in self.LOCAL_HOSTS:
            response = PlainTextResponse(
                "This test server only accepts connections from the computer it runs on.", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_gateway(settings: Settings):
    if settings.ai_engine == "claude-code":
        return ClaudeCodeGateway(settings.claude_code_path or find_claude_code(), settings.claude_code_model,
                                 settings.data_dir / "claude-code-workdir")
    return ClaudeGateway(settings.model)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def log_to_file(data_dir: FsPath) -> None:
    """Keep a server log on disk so problems can be diagnosed after the console window is closed."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "server.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)


def create_app(settings: Settings | None = None, gateway=None, practice_voice=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    if settings is None:
        settings = load_settings()
        log_to_file(settings.data_dir)
    gateway = gateway or build_gateway(settings)
    engine = getattr(gateway, "engine", "api")
    service = Service(settings, gateway)
    practice_voice = practice_voice or PracticeVoice(settings.data_dir / "voice")
    session_audio = SessionAudio(settings.data_dir, practice_voice, service.audio_job,
                                 on_expired=service.audio_expired, on_preview=service.preview_made,
                                 on_ready=service.session_ready)
    service.on_delivered = session_audio.start

    def sign_in_alert(failures: int) -> None:
        message = notify.sign_in_alert_email(settings, failures=failures)
        try:
            notify.send(settings, message, settings.data_dir / "outbox")
        except Exception:  # noqa: BLE001 - the pause itself still protects the dashboard
            log.exception("The sign-in alert email could not be sent")

    admin = AdminAuth(settings, service.db, alert=sign_in_alert)
    secure_cookie = settings.public_url.startswith("https://")
    # Fixed phrases are spoken in advance, so the greeting, the acknowledgements and what the consultant says when the
    # client is quiet play without delay.
    fixed_phrases = {lang: [*sentences(service.content["greeting"][lang]), *service.content["acknowledgements"][lang],
                            *(p for key in SILENCE_PHRASES for p in sentences(service.content[key][lang]))]
                     for lang in service.languages if lang in SPEECH_LANGUAGES}

    def voice_settings() -> dict:
        return service.db.get_setting("voice") or DEFAULT_VOICE

    # With the pay-as-you-go API, work a restart interrupted carries on by itself (a paying client never waits on a
    # button). With the test engine it stays stopped, so closing the server really stops using the subscription.
    carry_on = engine == "api"
    running: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        service.startup(carry_on=carry_on)
        if practice_voice.available:
            log.info("Practice voice: loading the recorded practice voice (Pocket TTS)")
        # Loaded before the first client is served, so nobody waits in silence while it loads.
        await asyncio.to_thread(practice_voice.start, fixed_phrases, 1.0)
        session_audio.resume()  # carry on with any session audio left unfinished when the app stopped
        # speech to text for spoken answers (each language's model), loaded while the app starts
        transcribe_module.warm_up(tuple(service.languages))
        if carry_on:
            for session_id, step in service.interrupted_work():
                log.info("Session %s: carrying on after the restart", session_id[:6])
                task = asyncio.create_task(step(session_id))
                running.add(task)
                task.add_done_callback(running.discard)

        async def hourly():  # old audio is deleted (kept for the retention period only); due emails are sent
            while True:
                for job, name in ((lambda: session_audio.cleanup(settings.audio_retention_days), "deleting old audio"),
                                  (service.send_due_emails, "sending reminder and follow-up emails"),
                                  (admin.cleanup, "removing ended admin sign-ins")):
                    try:
                        await asyncio.to_thread(job)
                    except Exception:  # noqa: BLE001 - try again at the next hour
                        log.exception("Hourly task failed: %s", name)
                await asyncio.sleep(3600)

        async def every_minute():  # a consultation left open past its time limit is closed, and its summary written
            while True:
                await asyncio.sleep(60)
                try:
                    for session_id in await asyncio.to_thread(service.expire_overdue_consultations):
                        task = asyncio.create_task(service.run_extraction(session_id))
                        running.add(task)
                        task.add_done_callback(running.discard)
                except Exception:  # noqa: BLE001 - try again in a minute
                    log.exception("Closing consultations past their time limit failed")

        cleaner = asyncio.create_task(hourly())
        timekeeper = asyncio.create_task(every_minute())
        if engine == "claude-code":
            log.warning("TEST ENGINE: running on your Claude subscription via Claude Code (%s). For your own "
                        "testing on this computer only - use made-up details, never real patients.", gateway.exe)
            if gateway.exe is None:
                log.error("Claude Code was not found: set CLAUDE_CODE_PATH in .env")
        elif not os.getenv("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY is not set: consultations will fail until it is added to .env")
        if not admin.enabled:
            log.warning("ADMIN_PASSWORD_HASH is not set: the admin dashboard is closed until you run "
                        "python -m app.set_admin_password")
        yield
        cleaner.cancel()
        timekeeper.cancel()
        session_audio.close()
        practice_voice.close()

    app = FastAPI(title="AI Hypnotherapist", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.add_middleware(SecurityHeaders, embed_origins=settings.embed_origins)
    app.add_middleware(RequestLog)
    if engine == "claude-code":
        app.add_middleware(LocalOnly)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def load_session(session_id: str) -> dict:
        session = service.db.get_session(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    def language_or_default(language: str) -> str:
        return language if language in service.languages else "en"

    async def speech_response(body: SpeechIn, session_rate: float | None = None, style: str | None = None,
                              language: str = "en"):
        """The practice voice speaking the text, streamed as raw 16-bit mono PCM while it is generated. The
        consultation keeps the voice's natural speed; in the session the pace and the gentle rise and settle follow
        the part being spoken (session_delivery), based on the clinician's session speed."""
        if body.kind == "session":
            base = session_rate if session_rate is not None else voice_settings()["session_rate"]
            rate, soften_ms = rate_for(style or SLOWER, base), fades_for(style or SLOWER)
        else:
            rate, soften_ms = 1.0, None
        chunks = practice_voice.stream(spoken_form(body.text), rate, soften_ms, language)
        try:
            first = await anext(chunks)  # a failure before any audio is a proper error, not a broken stream
        except StopAsyncIteration:
            first = b""
        except Exception as exc:
            log.warning("Practice voice failed: %s", exc)
            raise HTTPException(503, "the practice voice is not available") from exc

        async def body():
            yield first
            try:
                async for chunk in chunks:
                    yield chunk
            except Exception:
                log.exception("Practice voice failed while speaking")

        return StreamingResponse(body(), media_type="application/octet-stream",
                                 headers={"X-Sample-Rate": str(SAMPLE_RATE), "X-Accel-Buffering": "no"})

    # --- patient ---------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    def public_prices() -> dict:
        prices = service.prices()
        return {
            "currency": "GBP",
            "consultation": prices["consultation"],
            "ai_session": prices["ai_session"],
            "total": prices["consultation"] + prices["ai_session"],
            "human_session_from": prices["human_from"],
            "human_session_to": prices["human_to"],
        }

    @app.get("/api/prices")
    def get_prices(response: Response):
        """The current prices (as set in the admin dashboard), for the practice's website page."""
        response.headers["Cache-Control"] = "public, max-age=60"
        return public_prices()

    @app.get("/api/config")
    def get_config(language: str = "en"):
        language = language_or_default(language)
        return {
            "languages": [{"code": code, "name": service.content["language_labels"][code]}
                          for code in service.languages],
            "countries": service.crisis.options(language),
            "prices": public_prices(),
            "payment_provider": settings.payment_provider,
            "audio_retention_days": settings.audio_retention_days,
            "require_clinician_review": settings.require_clinician_review,
            "engine": engine,
            "voice": service.db.get_setting("voice") or DEFAULT_VOICE,
            "practice_voice": practice_voice.speaks(language),
            "transcribe": transcribe_module.usable(language),  # spoken answers are recorded and transcribed here
            "acknowledgements": service.content["acknowledgements"].get(language, []),
            # what the consultant says when the client is quiet: after a minute, and when it pauses
            "silence": {key: service.content[key].get(language, "") for key in SILENCE_PHRASES},
            # the websites that show the app inside their page: only they are told the open session's link
            "embed_origins": list(settings.embed_origins),
        }

    @app.get("/api/screening")
    def get_screening(language: str = "en"):
        language = language_or_default(language)
        return {"version": service.screening.version, "questions": service.screening.questions_for(language)}

    @app.post("/api/sessions")
    def start_session(body: StartSessionIn):
        if body.language not in service.languages:
            raise HTTPException(422, "unsupported language")
        if not service.crisis.is_valid(body.country):
            raise HTTPException(422, "unknown country")
        try:
            return service.start_session(country=body.country, language=body.language, answers=body.answers)
        except ScreeningError as exc:
            raise HTTPException(422, str(exc)) from exc

    def session_view(session: dict) -> dict:
        """What the client's page shows for the session, with its audio (preview and download) when there is any."""
        view = service.patient_view(session)
        if view["status"] in AUDIO_STATUSES and practice_voice.speaks(session["language"]):
            session_audio.start(session["id"])  # makes whatever the session needs now, if it is not made yet
            view["audio"] = {**session_audio.status(session["id"]), "expired": bool(session.get("audio_expired_at"))}
            for text in ("session_script", "session_plan"):  # the client gets the recording, not the text
                view.pop(text, None)
        return view

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: SessionId, background: BackgroundTasks):
        session = load_session(session_id)
        if service.expire_consultation(session) == "extracting":
            background.add_task(service.run_extraction, session_id)
        session = load_session(session_id)
        resume = service.background_to_resume(session)
        if resume:
            background.add_task(resume, session_id)
        return session_view(session)

    def after_payment(background: BackgroundTasks, session_id: str, purpose: str) -> None:
        """A completed payment: the receipt, and for the full session its writing starts."""
        background.add_task(service.send_receipt, session_id, purpose)
        if purpose == "session":
            background.add_task(service.run_generation, session_id)

    @app.post("/api/sessions/{session_id}/pay")
    def pay(session_id: SessionId, body: PaymentIn, background: BackgroundTasks):
        """The test checkout pays at once (no money moves). With Stripe, a saved card is charged at once; otherwise
        the answer is {"checkout_url"}: Stripe's secure payment page, which brings the client back afterwards."""
        try:
            result = service.pay(load_session(session_id), purpose=body.purpose, save_card=body.save_card,
                                 acknowledged=body.acknowledged, email=(body.email or "").strip() or None,
                                 emails_ok=body.emails_ok, amount_seen=body.amount)
        except Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except PaymentUnavailable as exc:
            log.error("Session %s: Stripe unavailable: %s", session_id[:6], exc)
            raise HTTPException(503, "the payment service is not available right now") from exc
        if "checkout_url" in result:
            return {"checkout_url": result["checkout_url"]}
        after_payment(background, session_id, body.purpose)
        return session_view(load_session(session_id))

    @app.post("/api/sessions/{session_id}/confirm-payment")
    def confirm_payment(session_id: SessionId, body: ConfirmPaymentIn, background: BackgroundTasks):
        """The client is back from Stripe's payment page: the app checks the payment with Stripe itself."""
        load_session(session_id)
        try:
            purpose = service.confirm_payment(session_id, body.checkout_id)
        except Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except PaymentUnavailable as exc:
            log.error("Session %s: Stripe unavailable: %s", session_id[:6], exc)
            raise HTTPException(503, "the payment service is not available right now") from exc
        if purpose:
            after_payment(background, session_id, purpose)
        return session_view(load_session(session_id))

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request, background: BackgroundTasks):
        """Stripe's notice that a payment page was paid (in case the client closed it before coming back)."""
        try:
            done = service.stripe_notice(await request.body(), request.headers.get("stripe-signature"))
        except (ValueError, Invalid) as exc:
            raise HTTPException(400, str(exc)) from exc
        except (Conflict, PaymentUnavailable) as exc:
            log.error("Stripe notice could not be completed: %s", exc)
            raise HTTPException(503, "try again later") from exc  # Stripe sends it again
        if done:
            after_payment(background, *done)
        return {"received": True}

    @app.post("/api/sessions/{session_id}/follow-up")
    def follow_up(session_id: SessionId, body: FollowUpIn):
        try:
            service.follow_up(load_session(session_id), severity=body.severity, comments=body.comments.strip())
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/stop-emails")
    def stop_emails(session_id: SessionId):
        service.stop_emails(load_session(session_id))
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/start-consultation")
    def start_consultation(session_id: SessionId):
        try:
            service.start_consultation(load_session(session_id))
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/pause")
    def pause_consultation(session_id: SessionId):
        """The client has been silent: the microphone stops, and so does their clock."""
        try:
            service.pause_consultation(load_session(session_id))
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/end-consultation")
    def end_consultation(session_id: SessionId, background: BackgroundTasks):
        """The client ends the consultation before its time is up."""
        try:
            to = service.end_consultation(load_session(session_id))
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        if to == "extracting":
            background.add_task(service.run_extraction, session_id)
        return session_view(load_session(session_id))

    @app.post("/api/sessions/{session_id}/resume")
    def resume_consultation(session_id: SessionId):
        session = load_session(session_id)
        if session["status"] != "consulting":
            raise HTTPException(409, "the consultation is not active")
        service.resume_consultation(session)
        return session_view(load_session(session_id))

    @app.post("/api/sessions/{session_id}/continue-consultation")
    def continue_consultation(session_id: SessionId):
        """A consultation that ended with too few answers goes on, once, for a little longer."""
        try:
            service.continue_consultation(load_session(session_id))
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return session_view(load_session(session_id))

    @app.post("/api/sessions/{session_id}/back-to-choice")
    def back_to_choice(session_id: SessionId):
        try:
            service.back_to_choice(load_session(session_id))
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/retry-preview")
    def retry_preview(session_id: SessionId, background: BackgroundTasks):
        try:
            service.retry_preview(load_session(session_id))
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.run_opening, session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/messages")
    async def post_message(session_id: SessionId, body: MessageIn):
        session = load_session(session_id)
        closed = service.expire_consultation(session)
        if closed:
            background = BackgroundTask(service.run_extraction, session_id) if closed == "extracting" else None
            return StreamingResponse(iter([sse("done", {"consultation_complete": True, "delivery": "warm"})]),
                                     media_type="text/event-stream", background=background)
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "the message is empty")
        if session["status"] != "consulting":
            raise HTTPException(409, "the consultation is not active")
        lock = service.lock_for(session_id)
        if lock.locked():
            raise HTTPException(409, "the previous message is still being processed")

        async def events():
            async with lock:
                try:
                    voice = body.voice.model_dump() if body.voice else None
                    async for event, payload in service.consultation_turn(session_id, text, voice):
                        yield sse(event, payload)
                except Exception:
                    # Headers are already sent, so end the stream with an error event instead of cutting it off.
                    log.exception("Session %s: consultation turn crashed", session_id)
                    yield sse("error", {"code": "ai_unavailable"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
            background=BackgroundTask(service.run_extraction, session_id),
        )

    @app.post("/api/sessions/{session_id}/speech")
    async def speak(session_id: SessionId, body: SpeechIn):
        session = load_session(session_id)
        if not practice_voice.speaks(session["language"]):
            raise HTTPException(404, "the practice voice is not available")
        if not speakable(body.text, service.speech_sources(session)):
            raise HTTPException(403, "only the practice's own words can be spoken in its voice")
        style = service.session_style(session, spoken_form(body.text), body.position) if body.kind == "session" else None
        return await speech_response(body, style=style, language=session["language"])

    @app.post("/api/sessions/{session_id}/transcribe")
    async def transcribe_audio(session_id: SessionId, request: Request):
        """The client's spoken answer, recorded by the browser and sent as audio, turned into words on this
        computer (app/transcribe.py). Used instead of the browser's own speech recognition, which only works
        reliably in Edge and not at all over plain HTTP in Chrome, so a phone or any other browser can still
        answer aloud. The audio is transcribed in memory and never written to disk."""
        session = load_session(session_id)
        if not transcribe_module.available(session["language"]):
            raise HTTPException(503, "speech to text is not available")
        audio = await request.body()
        language = session["language"]  # the language the client chose: Whisper is told it, never left to guess
        try:
            text = await asyncio.to_thread(transcribe_module.transcribe, audio, language)
        except transcribe_module.Invalid as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"text": text}

    def audio_file(path: FsPath) -> FileResponse:
        """Served in parts (range requests), so playback starts before a long file has arrived."""
        return FileResponse(path, media_type=AUDIO_TYPES[path.suffix.lower()],
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/sessions/{session_id}/background")
    def session_background(session_id: SessionId):
        session = load_session(session_id)
        background = service.session_background()
        if session["status"] != "ready" or background is None:
            raise HTTPException(404, "no background music for this session")
        return audio_file(background["path"])

    @app.get("/api/sessions/{session_id}/preview")
    def session_preview(session_id: SessionId):
        load_session(session_id)
        preview = session_audio.files(session_id)["preview"]
        if preview is None:
            raise HTTPException(404, "the preview is not ready yet")
        return FileResponse(preview, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/sessions/{session_id}/download")
    def session_download(session_id: SessionId):
        session = load_session(session_id)
        if session["status"] != "ready":  # only a paid-for session is ever written in full
            raise HTTPException(402, "the full session is available after payment")
        audio = session_audio.download(session_id)
        if audio is None:
            raise HTTPException(404, "the session is still being prepared")
        return FileResponse(audio, media_type="audio/mpeg", filename="Hypnotherapy session.mp3",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/sessions/{session_id}/retry-generation")
    def retry_generation(session_id: SessionId, background: BackgroundTasks):
        load_session(session_id)
        try:
            service.retry_generation(session_id)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.run_generation, session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/retry-extraction")
    def retry_extraction(session_id: SessionId, background: BackgroundTasks):
        load_session(session_id)
        try:
            service.retry_extraction(session_id)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.run_extraction, session_id)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/ai-session")
    def choose_ai_session(session_id: SessionId, body: AISessionIn):
        """The client chooses the full AI session: next they pay for it (the script is finished after payment)."""
        try:
            service.choose_ai_session(load_session(session_id), body.baseline_severity)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/human-request")
    def request_human(session_id: SessionId, body: HumanRequestIn, background: BackgroundTasks):
        fields = {"name": body.name.strip(), "email": body.email.strip(), "phone": body.phone.strip(),
                  "description": body.description.strip()}
        if not all(fields.values()):
            raise HTTPException(422, "please fill in every field")
        try:
            service.request_human(load_session(session_id), **fields, baseline_severity=body.baseline_severity)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.notify_human_request, session_id)  # emailed to the practice
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/feedback")
    def give_feedback(session_id: SessionId, body: FeedbackIn):
        comments = body.comments.strip()
        if not comments:
            raise HTTPException(422, "the feedback is empty")
        try:
            service.add_feedback(load_session(session_id), rating=body.rating,
                                 severity_after=body.severity_after, comments=comments)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    # --- admin (the practice): sign-in, then the dashboard ----------------------------------------------------

    def signed_in(request: Request) -> bool:
        return admin.session(request.cookies.get(COOKIE, "")) is not None

    def own_page_request(request: Request) -> None:
        """Every change must come from the dashboard's own scripts: other sites cannot add this header."""
        if request.headers.get(REQUEST_HEADER[0]) != REQUEST_HEADER[1]:
            raise HTTPException(403, "this request must come from the admin dashboard")

    def require_admin(request: Request) -> None:
        if not signed_in(request):
            raise HTTPException(401, "please sign in")
        if request.method not in ("GET", "HEAD"):
            own_page_request(request)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/sign-in", include_in_schema=False)
    def sign_in_page(request: Request):
        if signed_in(request):
            return RedirectResponse("/admin", status_code=303)
        return FileResponse(STATIC_DIR / "sign-in.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page(request: Request):
        if not signed_in(request):
            return RedirectResponse("/sign-in", status_code=303)
        return FileResponse(STATIC_DIR / "clinician.html")

    @app.get("/clinician", include_in_schema=False)
    def old_dashboard_address():
        return RedirectResponse("/admin", status_code=308)

    @app.get("/api/admin/robot-check")
    def robot_check():
        return admin.robot.challenge()

    @app.post("/api/admin/sign-in", dependencies=[Depends(own_page_request)])
    def sign_in(body: SignInIn, request: Request, response: Response):
        try:
            token = admin.sign_in(email=body.email, password=body.password, robot=body.robot, ip=client_ip(request),
                                  user_agent=request.headers.get("user-agent", ""))
        except SignInRefused as exc:
            log.warning("Admin sign-in refused (%s) from %s", exc.status, client_ip(request))
            raise HTTPException(exc.status, exc.message) from exc
        log.info("Admin signed in from %s", client_ip(request))
        response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="strict",
                            secure=secure_cookie, path="/")
        return {"ok": True}

    @app.post("/api/admin/sign-out", dependencies=[Depends(own_page_request)])
    def sign_out(request: Request, response: Response):
        admin.sign_out(request.cookies.get(COOKIE, ""))
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict", secure=secure_cookie)
        return {"ok": True}

    clinician = APIRouter(prefix="/api/clinician", dependencies=[Depends(require_admin)])

    @clinician.get("/admin")
    def admin_settings():
        s = settings
        return {
            "prices": service.prices(),
            "default_prices": {"consultation": s.consultation_price_gbp, "ai_session": s.ai_session_price_gbp,
                               "human_from": s.human_session_price_from_gbp, "human_to": s.human_session_price_to_gbp},
            "instructions": service.instructions(),
            "admin_email": s.admin_email,
            "sign_ins": service.db.recent_sign_ins(10),
        }

    @clinician.put("/prices")
    def save_prices(body: PricesIn):
        try:
            service.save_prices(body.model_dump())
        except Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"ok": True, "prices": service.prices()}

    @clinician.put("/instructions")
    def save_instructions(body: InstructionsIn):
        service.save_instructions(body.model_dump())
        return {"ok": True, "instructions": service.instructions()}

    # Books the practice adds to improve the AI (see books.py).

    def book_or_404(book_id: int, with_notes: bool = False) -> dict:
        book = service.books.get(book_id, with_notes)
        if book is None:
            raise HTTPException(404, "book not found")
        return book

    @clinician.get("/books")
    def list_books():
        return {"books": service.books.list(), "engine": engine, "max_upload_mb": MAX_UPLOAD_BYTES // 2**20}

    @clinician.get("/books/{book_id}")
    def book_detail(book_id: int):
        return book_or_404(book_id, with_notes=True)

    @clinician.post("/books")
    async def upload_book(request: Request, background: BackgroundTasks,
                          name: str = Query(min_length=1, max_length=200)):
        """The PDF itself is the request body (read in pieces, so a large book never fills the memory)."""
        filename = FsPath(name.replace("\\", "/")).name.strip()
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(422, "Please choose a PDF file.")
        incoming = settings.data_dir / "library" / "uploads" / f"incoming-{secrets.token_hex(8)}.pdf"
        incoming.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with incoming.open("wb") as out:
                async for piece in request.stream():
                    size += len(piece)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"This file is larger than {MAX_UPLOAD_BYTES // 2**20} MB.")
                    out.write(piece)
            with incoming.open("rb") as check:
                if check.read(5) != b"%PDF-":
                    raise HTTPException(422, "This file is not a PDF.")
            book_id = service.books.add(filename, incoming)
        except BookConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            incoming.unlink(missing_ok=True)  # moved into the library when it was accepted
        background.add_task(service.books.read, book_id)
        return book_or_404(book_id)

    @clinician.post("/books/{book_id}/start")
    def start_book(book_id: int, background: BackgroundTasks):
        try:
            service.books.start(book_id)
        except BookConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.books.process, book_id)
        return book_or_404(book_id)

    @clinician.post("/books/{book_id}/approve")
    def approve_book(book_id: int):
        try:
            service.books.approve(book_id)
        except BookConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return book_or_404(book_id)

    @clinician.delete("/books/{book_id}")
    def remove_book(book_id: int):
        try:
            service.books.remove(book_id)
        except BookConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @clinician.get("/sessions")
    def list_sessions():
        return service.db.list_sessions()

    @clinician.get("/sessions/{session_id}")
    def session_detail(session_id: SessionId):
        view = service.clinician_view(session_id)
        if view is None:
            raise HTTPException(404, "session not found")
        return view

    @clinician.post("/sessions/{session_id}/review")
    def review(session_id: SessionId, body: ReviewIn):
        load_session(session_id)
        try:
            service.review(session_id, decision=body.decision, final_text=body.final_text, notes=body.notes.strip())
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @clinician.post("/sessions/{session_id}/regenerate")
    def regenerate(session_id: SessionId, background: BackgroundTasks):
        load_session(session_id)
        try:
            service.regenerate(session_id)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(service.run_generation, session_id)
        return {"ok": True}

    @clinician.get("/lessons")
    def list_lessons():
        return service.db.list_lessons()

    @clinician.post("/lessons/propose")
    async def propose():
        try:
            created = await service.propose_lessons()
        except (LLMUnavailable, LLMRefusal) as exc:
            raise HTTPException(503, f"Could not generate lessons: {exc}") from exc
        return {"created": created}

    @clinician.post("/lessons/{lesson_id}/decision")
    def decide_lesson(lesson_id: int, body: LessonDecisionIn):
        decision = "approved" if body.decision == "approve" else "rejected"
        if not service.db.decide_lesson(lesson_id, decision):
            raise HTTPException(409, "only a proposed lesson can be decided")
        return {"ok": True}

    @clinician.get("/voice")
    def get_voice():
        saved = service.db.get_setting("voice")
        return {
            "settings": saved or DEFAULT_VOICE,
            "saved": saved is not None,
            "practice_voice": {
                "installed": pocket_tts_installed(),
                "prepared": practice_voice.state_path.exists(),
                "available": practice_voice.available,
                "problem": practice_voice.failed,
                # the languages the voice is prepared for and ready in (others use the device's own voice)
                "languages": [code for code in SPEECH_LANGUAGES if practice_voice.speaks(code)],
                "language_problems": practice_voice.failed_languages,
            },
        }

    @clinician.post("/voice/speak")
    async def speak_as_practice(body: PreviewSpeechIn):
        """The clinician hearing any words of their choice in their own voice (a preview), at a saved or
        a trial speed."""
        if not practice_voice.available:
            raise HTTPException(404, "the practice voice is not available")
        return await speech_response(body, body.rate)

    @clinician.put("/voice")
    def save_voice(body: VoiceSettingsIn):
        service.db.set_setting("voice", body.model_dump())
        return {"ok": True}

    @clinician.get("/background")
    def get_background():
        chosen = service.db.get_setting("session_background") or {}
        return {"file": chosen.get("file"), "volume": chosen.get("volume", DEFAULT_BACKGROUND_VOLUME),
                "files": service.audio_files()}

    @clinician.put("/background")
    def save_background(body: BackgroundIn):
        if body.file is not None and body.file not in service.audio_files():
            raise HTTPException(422, "choose one of the audio files in data/voice")
        service.db.set_setting("session_background", body.model_dump())
        return {"ok": True}

    @clinician.get("/background/audio")
    def preview_background(file: str):
        if file not in service.audio_files():
            raise HTTPException(404, "no such audio file")
        return audio_file(settings.data_dir / "voice" / file)

    @clinician.get("/reverb")
    def get_reverb():
        return {"amount": service.session_reverb()}

    @clinician.put("/reverb")
    def save_reverb(body: ReverbIn):
        service.db.set_setting("session_reverb", body.model_dump())
        return {"ok": True}

    @clinician.get("/knowledge")
    def knowledge():
        try:
            return service.knowledge_status()
        except KnowledgeBaseError as exc:
            raise HTTPException(500, str(exc)) from exc

    app.include_router(clinician)
    return app
