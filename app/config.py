import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
CONTENT_DIR = APP_DIR / "content"
PROMPTS_DIR = APP_DIR / "prompts"
STATIC_DIR = APP_DIR / "static"

load_dotenv(PROJECT_ROOT / ".env")

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
AI_ENGINES = ("api", "claude-code")
PAYMENT_PROVIDERS = ("test", "stripe")  # test: no money moves; stripe: real card payments (see payments.py)


@dataclass(frozen=True)
class Settings:
    ai_engine: str
    claude_code_path: Path | None
    claude_code_model: str
    model: str
    knowledge_root: Path
    data_dir: Path
    admin_password_hash: str  # only a hash of the admin's password (see app/set_admin_password.py)
    require_clinician_review: bool
    consultation_price_gbp: int
    ai_session_price_gbp: int
    human_session_price_from_gbp: int
    human_session_price_to_gbp: int
    consultation_effort: str
    risk_effort: str
    extraction_effort: str
    script_effort: str
    soft_turn_limit: int
    hard_turn_limit: int
    consultation_minutes: int
    payment_provider: str = "test"  # "test": a clearly marked test checkout, no money moves; "stripe": real cards
    stripe_secret_key: str = ""  # sk_test_... while testing, sk_live_... for real payments
    stripe_webhook_secret: str = ""  # whsec_...: checks that payment notifications really come from Stripe
    audio_retention_days: int = 7  # every session's audio is deleted this many days after it was made
    notify_email: str = "practice@example.com"  # where hypnotherapist requests are sent
    smtp_host: str = ""  # empty: requests are saved to data/outbox instead of being emailed
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    public_url: str = "http://127.0.0.1:8000"  # links in emails (to the practice and to clients) point here
    practice_name: str = "Example Practice"  # the seller named on receipts
    practice_address: str = ""  # the practice's postal address, required on UK receipts: set PRACTICE_ADDRESS
    preview_reminder_days: tuple[int, ...] = (1, 6)  # reminders this many days after an undecided preview
    follow_up_days: int = 10  # "how are you doing?" this many days after the full session is ready
    admin_email: str = "practice@example.com"  # the only email that can sign in to the admin dashboard
    robot_check_max_number: int = 100_000  # the robot check's work: about a second on a person's device
    client_page_url: str = ""  # the page clients use the app on (the practice's website): their links go there
    embed_origins: tuple[str, ...] = ()  # websites allowed to show the app inside their page (never the admin)
    consultation_min_answers: int = 10  # a session is written from at least this many answers...
    consultation_min_words: int = 600  # ...or fewer, longer answers with at least this many words in all
    offer_ai_when_unsuitable: bool = False  # True: the AI session is offered even when the summary advises against
    languages: tuple[str, ...] = ("en", "de", "fr", "es")  # the languages clients can choose (English always)
    books_model: str = "claude-haiku-4-5"  # reads books added in the admin panel: about 4 times cheaper per book

    def client_page(self) -> str:
        """Where clients' private links and the return from payment lead (the app itself unless it is shown on
        the practice's website)."""
        return self.client_page_url or f"{self.public_url}/"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _effort(name: str, default: str) -> str:
    value = (os.getenv(name, "").strip().lower() or default)
    if value not in EFFORT_LEVELS:
        raise ValueError(f"{name} must be one of {EFFORT_LEVELS}, got {value!r}")
    return value


def load_settings() -> Settings:
    soft = _int("CONSULTATION_SOFT_TURN_LIMIT", 28)
    hard = _int("CONSULTATION_HARD_TURN_LIMIT", 36)
    if not 0 < soft < hard:
        raise ValueError("CONSULTATION_SOFT_TURN_LIMIT must be positive and below CONSULTATION_HARD_TURN_LIMIT")
    engine = os.getenv("AI_ENGINE", "").strip().lower() or "api"
    if engine not in AI_ENGINES:
        raise ValueError(f"AI_ENGINE must be one of {AI_ENGINES}, got {engine!r}")
    claude_code_path = os.getenv("CLAUDE_CODE_PATH", "").strip()
    return Settings(
        ai_engine=engine,
        claude_code_path=Path(claude_code_path) if claude_code_path else None,
        claude_code_model=os.getenv("CLAUDE_CODE_MODEL", "").strip() or "claude-opus-5-5",
        model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5-5",
        knowledge_root=Path(os.getenv("KNOWLEDGE_ROOT", "").strip() or "knowledge"),
        data_dir=Path(os.getenv("DATA_DIR", "").strip() or PROJECT_ROOT / "data"),
        admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", "").strip(),
        require_clinician_review=_bool("REQUIRE_CLINICIAN_REVIEW", False),  # scripts go straight to the client
        consultation_price_gbp=_int("CONSULTATION_PRICE_GBP", 5),
        ai_session_price_gbp=_int("AI_SESSION_PRICE_GBP", 35),
        human_session_price_from_gbp=_int("HUMAN_SESSION_PRICE_FROM_GBP", 199),
        human_session_price_to_gbp=_int("HUMAN_SESSION_PRICE_TO_GBP", 299),
        consultation_effort=_effort("CONSULTATION_EFFORT", "medium"),
        risk_effort=_effort("RISK_EFFORT", "medium"),
        extraction_effort=_effort("EXTRACTION_EFFORT", "high"),
        script_effort=_effort("SCRIPT_EFFORT", "medium"),  # a one-hour script within about 3 minutes
        soft_turn_limit=soft,
        hard_turn_limit=hard,
        consultation_minutes=_int("CONSULTATION_MINUTES", 20),
        payment_provider=_payment_provider(),
        stripe_secret_key=os.getenv("STRIPE_SECRET_KEY", "").strip(),
        stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", "").strip(),
        audio_retention_days=_int("AUDIO_RETENTION_DAYS", 7),
        notify_email=os.getenv("NOTIFY_EMAIL", "").strip() or "practice@example.com",
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", "").strip(),
        public_url=(os.getenv("PUBLIC_URL", "").strip() or "http://127.0.0.1:8000").rstrip("/"),
        practice_name=os.getenv("PRACTICE_NAME", "").strip() or "Example Practice",
        practice_address=os.getenv("PRACTICE_ADDRESS", "").strip(),
        preview_reminder_days=tuple(int(day) for day in (os.getenv("PREVIEW_REMINDER_DAYS", "").strip() or "1,6")
                                    .split(",") if day.strip()),
        follow_up_days=_int("FOLLOW_UP_DAYS", 10),
        admin_email=(os.getenv("ADMIN_EMAIL", "").strip() or os.getenv("NOTIFY_EMAIL", "").strip()
                     or "practice@example.com").lower(),
        client_page_url=os.getenv("CLIENT_PAGE_URL", "").strip(),
        embed_origins=tuple(origin.strip().rstrip("/") for origin in os.getenv("EMBED_ORIGINS", "").split(",")
                            if origin.strip()),
        consultation_min_answers=max(1, _int("CONSULTATION_MIN_ANSWERS", 10)),
        consultation_min_words=max(0, _int("CONSULTATION_MIN_WORDS", 600)),
        offer_ai_when_unsuitable=_bool("OFFER_AI_WHEN_UNSUITABLE", False),
        languages=_languages(),
        books_model=os.getenv("BOOKS_MODEL", "").strip() or "claude-haiku-4-5",
    )


def _languages() -> tuple[str, ...]:
    """LANGUAGES: the languages clients can choose, in the order shown (English is always included)."""
    codes = [code.strip().lower() for code in (os.getenv("LANGUAGES", "").strip() or "en,de,fr,es").split(",")]
    codes = [code for code in codes if code]
    return tuple(dict.fromkeys(["en", *codes] if "en" not in codes else codes))


def _payment_provider() -> str:
    value = os.getenv("PAYMENT_PROVIDER", "").strip().lower() or "test"
    if value not in PAYMENT_PROVIDERS:
        raise ValueError(f"PAYMENT_PROVIDER must be one of {PAYMENT_PROVIDERS}, got {value!r}")
    return value
