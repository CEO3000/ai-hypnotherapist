import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    country TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL,
    screening_version TEXT NOT NULL,
    screening_answers TEXT NOT NULL,
    screening_outcome TEXT NOT NULL,
    stop_detail TEXT,
    coverage_note TEXT,
    intake TEXT,
    intake_meta TEXT,
    baseline_severity INTEGER,
    consultation_started_at TEXT,
    opening_status TEXT,
    audio_expired_at TEXT,
    client_email TEXT,
    emails_ok INTEGER,
    preview_ready_at TEXT,
    delivered_at TEXT,
    consultation_opened_at TEXT,
    paused_at TEXT,
    paused_seconds INTEGER,
    extra_minutes INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('patient', 'assistant')),
    content TEXT NOT NULL,
    voice TEXT
);
CREATE TABLE IF NOT EXISTS answer_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    created_at TEXT NOT NULL,
    analysis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id),
    level TEXT NOT NULL,
    categories TEXT NOT NULL,
    evidence TEXT NOT NULL,
    rationale TEXT NOT NULL,
    keyword_hits TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    knowledge_sources TEXT NOT NULL,
    lessons_used TEXT NOT NULL,
    draft TEXT NOT NULL,
    validation_issues TEXT NOT NULL,
    usage TEXT NOT NULL,
    final_text TEXT,
    review_decision TEXT,
    review_notes TEXT,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS openings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    text TEXT NOT NULL,
    validation_issues TEXT NOT NULL,
    usage TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('consultation', 'session')),
    amount_pence INTEGER NOT NULL,
    currency TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'refunded')),
    save_card INTEGER NOT NULL DEFAULT 0,
    paid_at TEXT,
    acknowledgement TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sent', 'saved', 'failed'))
);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    severity INTEGER NOT NULL,
    comments TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    rating INTEGER,
    severity_after INTEGER,
    comments TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    preferred_times TEXT NOT NULL,
    origin TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    lesson TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected')),
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    session_id TEXT,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    stop_reason TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    request_id TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    password_tag TEXT NOT NULL,
    ip TEXT NOT NULL,
    user_agent TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sign_ins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ip TEXT NOT NULL,
    ok INTEGER NOT NULL,
    user_agent TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_sign_ins ON admin_sign_ins(ip, at);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    pages INTEGER,
    words INTEGER,
    parts INTEGER NOT NULL DEFAULT 0,
    parts_done INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    author TEXT,
    category TEXT,
    topics TEXT,
    reason TEXT,
    notes TEXT,
    error TEXT,
    approved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_risk_session ON risk_events(session_id);
CREATE INDEX IF NOT EXISTS idx_scripts_session ON scripts(session_id, id);
CREATE INDEX IF NOT EXISTS idx_openings_session ON openings(session_id, id);
CREATE INDEX IF NOT EXISTS idx_payments_session ON payments(session_id, id);
CREATE INDEX IF NOT EXISTS idx_emails_session ON emails(session_id, kind);
"""

# Columns added after the first databases were created: (table, column, definition).
ADDED_COLUMNS = [
    ("messages", "voice", "TEXT"),
    ("sessions", "consultation_started_at", "TEXT"),
    ("sessions", "opening_status", "TEXT"),
    ("sessions", "audio_expired_at", "TEXT"),
    ("sessions", "client_email", "TEXT"),
    ("sessions", "emails_ok", "INTEGER"),
    ("sessions", "preview_ready_at", "TEXT"),
    ("sessions", "delivered_at", "TEXT"),
    ("sessions", "consultation_opened_at", "TEXT"),  # the client pressed Start (the clock starts at their first answer)
    ("sessions", "paused_at", "TEXT"),  # the consultation is paused (the client was silent): its clock stands still
    ("sessions", "paused_seconds", "INTEGER"),  # paused time so far, which does not count towards the time limit
    ("sessions", "extra_minutes", "INTEGER"),  # time added when a short consultation was continued
    ("payments", "acknowledgement", "TEXT"),
    ("human_requests", "description", "TEXT NOT NULL DEFAULT ''"),
    ("payments", "customer_ref", "TEXT"),  # Stripe: the customer a saved card belongs to
    ("payments", "method_ref", "TEXT"),  # Stripe: the saved card itself
]

JSON_FIELDS = {
    "screening_answers", "screening_outcome", "stop_detail", "intake", "intake_meta", "voice",
    "knowledge_sources", "lessons_used", "validation_issues", "usage",
    "categories", "keyword_hits", "topics",
}

# Background work a server restart interrupted: marked failed (the client can start it again) unless the app
# carries it on by itself (see Service.startup).
STALE_STATUSES = {"extracting": "extraction_failed", "generating": "generation_failed"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = {}
    for key in row.keys():
        value = row[key]
        if key in JSON_FIELDS and value is not None:
            value = json.loads(value)
        out[key] = value
    return out


def _encode(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self, mark_interrupted_failed: bool = True) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            for table, column, definition in ADDED_COLUMNS:  # databases created before these existed
                if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # Databases from before feedback became a comment alone (rating and score were required then).
            if any(row["name"] == "rating" and row["notnull"] for row in conn.execute("PRAGMA table_info(feedback)")):
                conn.executescript("""
                    CREATE TABLE feedback_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        created_at TEXT NOT NULL,
                        rating INTEGER,
                        severity_after INTEGER,
                        comments TEXT NOT NULL
                    );
                    INSERT INTO feedback_new SELECT id, session_id, created_at, rating, severity_after, comments FROM feedback;
                    DROP TABLE feedback;
                    ALTER TABLE feedback_new RENAME TO feedback;
                """)
            for stale, failed in (STALE_STATUSES.items() if mark_interrupted_failed else ()):
                conn.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE status = ?",
                    (failed, now(), stale),
                )

    # --- sessions -------------------------------------------------------

    def create_session(self, *, session_id, country, language, status, screening_version,
                       screening_answers, screening_outcome) -> None:
        ts = now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at, country, language, status, "
                "screening_version, screening_answers, screening_outcome) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, ts, ts, country, language, status, screening_version,
                 _encode(screening_answers), _encode(screening_outcome)),
            )

    def get_session(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return _decode(row)

    def transition(self, session_id: str, from_statuses: tuple[str, ...], to_status: str, **fields) -> bool:
        """Atomically move a session between statuses; False if it was not in an allowed status."""
        fields = {"status": to_status, "updated_at": now(), **fields}
        columns = ", ".join(f"{key} = ?" for key in fields)
        placeholders = ", ".join("?" for _ in from_statuses)
        with self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE sessions SET {columns} WHERE id = ? AND status IN ({placeholders})",
                [*(_encode(v) for v in fields.values()), session_id, *from_statuses],
            )
            return cursor.rowcount == 1

    def list_sessions(self, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.created_at, s.updated_at, s.country, s.status, s.screening_outcome,
                       s.stop_detail,
                       json_extract(s.intake, '$.presenting_issue') AS presenting_issue,
                       json_array_length(s.intake, '$.consistency_review') AS inconsistencies,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND m.role = 'patient')
                           AS patient_messages,
                       (SELECT COUNT(*) FROM risk_events r WHERE r.session_id = s.id AND r.level NOT IN ('check', 'confirm'))
                           AS risk_events,
                       (SELECT COUNT(*) FROM risk_events r WHERE r.session_id = s.id AND r.level IN ('check', 'confirm'))
                           AS safety_checks
                FROM sessions s ORDER BY s.created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_decode(r) for r in rows]

    # --- messages & risk -------------------------------------------------

    def add_message(self, session_id: str, role: str, content: str, voice: dict | None = None) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, created_at, role, content, voice) VALUES (?,?,?,?,?)",
                (session_id, now(), role, content, _encode(voice)),
            )
            return cursor.lastrowid

    def list_messages(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, role, content, voice FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [_decode(r) for r in rows]

    def add_answer_analysis(self, *, session_id: str, message_id: int, analysis: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO answer_analyses (session_id, message_id, created_at, analysis) VALUES (?,?,?,?)",
                (session_id, message_id, now(), _encode(analysis)),
            )

    def answer_analyses(self, session_id: str) -> dict[int, dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT message_id, analysis FROM answer_analyses WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return {row["message_id"]: json.loads(row["analysis"]) for row in rows}

    def add_risk_event(self, *, session_id, message_id, level, categories, evidence, rationale,
                       keyword_hits, source) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO risk_events (session_id, created_at, message_id, level, categories, evidence, "
                "rationale, keyword_hits, source) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, now(), message_id, level, _encode(categories), evidence, rationale,
                 _encode(keyword_hits), source),
            )

    def list_risk_events(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM risk_events WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [_decode(r) for r in rows]

    # --- scripts --------------------------------------------------------

    def add_script(self, *, session_id, model, prompt_version, knowledge_sources, lessons_used, draft,
                   validation_issues, usage) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO scripts (session_id, created_at, model, prompt_version, knowledge_sources, "
                "lessons_used, draft, validation_issues, usage) VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, now(), model, prompt_version, _encode(knowledge_sources), _encode(lessons_used),
                 draft, _encode(validation_issues), _encode(usage)),
            )
            return cursor.lastrowid

    def latest_script(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM scripts WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
            ).fetchone()
        return _decode(row)

    def review_script(self, script_id: int, *, decision: str, final_text: str | None, notes: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE scripts SET review_decision = ?, final_text = ?, review_notes = ?, reviewed_at = ? "
                "WHERE id = ?",
                (decision, final_text, notes, now(), script_id),
            )

    def mark_script_auto_approved(self, script_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE scripts SET review_decision = 'auto', final_text = draft, reviewed_at = ? WHERE id = ?",
                (now(), script_id),
            )

    def reviewed_scripts(self, limit: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT sc.id AS script_id, sc.session_id, sc.draft, sc.final_text, sc.review_decision,
                       sc.review_notes, f.rating, f.severity_after, f.comments, s.baseline_severity
                FROM scripts sc
                JOIN sessions s ON s.id = sc.session_id
                LEFT JOIN feedback f ON f.session_id = sc.session_id
                WHERE sc.review_decision IN ('approved', 'rejected')
                ORDER BY sc.reviewed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_decode(r) for r in rows]

    # --- feedback & human requests ---------------------------------------

    def add_feedback(self, *, session_id, rating, severity_after, comments) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO feedback (session_id, created_at, rating, severity_after, comments) VALUES (?,?,?,?,?)",
                (session_id, now(), rating, severity_after, comments),
            )

    def get_feedback(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
            ).fetchone()
        return _decode(row)

    def add_human_request(self, *, session_id, name, email, phone, description, origin,
                          preferred_times: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO human_requests (session_id, created_at, name, email, phone, preferred_times, origin, "
                "description) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, now(), name, email, phone, preferred_times, origin, description),
            )

    def get_human_request(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_requests WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
            ).fetchone()
        return _decode(row)

    # --- session openings (the start of a script, written before the session is paid for) -------------

    def add_opening(self, *, session_id, model, prompt_version, text, validation_issues, usage) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO openings (session_id, created_at, model, prompt_version, text, validation_issues, usage) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_id, now(), model, prompt_version, text, _encode(validation_issues), _encode(usage)),
            )
            return cursor.lastrowid

    def latest_opening(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM openings WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
            ).fetchone()
        return _decode(row)

    def set_session_fields(self, session_id: str, **fields) -> None:
        """Updates fields that are not part of the session's status (e.g. opening_status)."""
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE sessions SET {columns} WHERE id = ?",
                         [*(_encode(v) for v in fields.values()), session_id])

    # --- payments -----------------------------------------------------------

    def add_payment(self, *, session_id, purpose, amount_pence, currency, provider, status, save_card=False,
                    provider_ref=None, acknowledgement=None) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO payments (session_id, created_at, purpose, amount_pence, currency, provider, "
                "provider_ref, status, save_card, paid_at, acknowledgement) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, now(), purpose, amount_pence, currency, provider, provider_ref, status,
                 int(save_card), now() if status == "paid" else None, acknowledgement),
            )
            return cursor.lastrowid

    # --- emails to the client, and their follow-up -------------------------------------------------

    def log_email(self, *, session_id, kind, recipient, status) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO emails (session_id, created_at, kind, recipient, status) VALUES (?,?,?,?,?)",
                         (session_id, now(), kind, recipient, status))

    def list_emails(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM emails WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        return [_decode(r) for r in rows]

    def sessions_in(self, statuses: tuple[str, ...]) -> list[dict]:
        placeholders = ", ".join("?" for _ in statuses)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM sessions WHERE status IN ({placeholders})", statuses).fetchall()
        return [_decode(r) for r in rows]

    def sessions_for_emails(self, statuses: tuple[str, ...]) -> list[dict]:
        """Sessions in these statuses whose client gave an email address and still accepts emails."""
        placeholders = ", ".join("?" for _ in statuses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE status IN ({placeholders}) AND client_email IS NOT NULL "
                "AND emails_ok = 1", statuses).fetchall()
        return [_decode(r) for r in rows]

    def add_followup(self, *, session_id, severity, comments) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO followups (session_id, created_at, severity, comments) VALUES (?,?,?,?)",
                         (session_id, now(), severity, comments))

    def get_followup(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM followups WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                               (session_id,)).fetchone()
        return _decode(row)

    def payment_by_ref(self, provider_ref: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE provider_ref = ? ORDER BY id DESC LIMIT 1",
                               (provider_ref,)).fetchone()
        return _decode(row)

    def mark_payment_paid(self, payment_id: int, **refs) -> bool:
        """Pending -> paid (only once: the client's return and Stripe's notice can both arrive). refs: provider_ref,
        customer_ref, method_ref."""
        fields = {"status": "paid", "paid_at": now(), **{k: v for k, v in refs.items() if v}}
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as conn:
            cursor = conn.execute(f"UPDATE payments SET {columns} WHERE id = ? AND status = 'pending'",
                                  [*fields.values(), payment_id])
            return cursor.rowcount == 1

    def mark_payment_failed(self, payment_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE payments SET status = 'failed' WHERE id = ? AND status = 'pending'", (payment_id,))

    def saved_card(self, session_id: str) -> dict | None:
        """The paid payment whose card the client chose to save (with Stripe's references to it), if any."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE session_id = ? AND status = 'paid' AND save_card = 1 "
                               "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return _decode(row)

    def list_payments(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM payments WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        return [_decode(r) for r in rows]

    # --- lessons --------------------------------------------------------

    def add_lesson(self, *, lesson, rationale, evidence_ids) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO lessons (created_at, lesson, rationale, evidence, status) VALUES (?,?,?,?, 'proposed')",
                (now(), lesson, rationale, _encode(evidence_ids)),
            )
            return cursor.lastrowid

    def list_lessons(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM lessons ORDER BY id DESC").fetchall()
        lessons = []
        for row in rows:
            item = _decode(row)
            item["evidence"] = json.loads(item["evidence"])
            lessons.append(item)
        return lessons

    def decide_lesson(self, lesson_id: int, decision: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE lessons SET status = ?, decided_at = ? WHERE id = ? AND status = 'proposed'",
                (decision, now(), lesson_id),
            )
            return cursor.rowcount == 1

    def approved_lessons(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, lesson FROM lessons WHERE status = 'approved' ORDER BY id"
            ).fetchall()
        return [_decode(r) for r in rows]

    # --- practice settings --------------------------------------------------

    def get_setting(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, _encode(value), now()),
            )

    # --- admin sign-in (see admin_auth.py) ------------------------------------------------------------------

    def add_sign_in(self, *, ip: str, ok: bool, user_agent: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO admin_sign_ins (at, ip, ok, user_agent) VALUES (?,?,?,?)",
                         (now(), ip, int(ok), user_agent))

    def sign_in_failures(self, *, since: str, ip: str | None = None) -> list[str]:
        """When the wrong passwords since then were given (newest first), from one address or from anywhere."""
        query = "SELECT at FROM admin_sign_ins WHERE ok = 0 AND at > ?" + (" AND ip = ?" if ip is not None else "")
        with self.connect() as conn:
            rows = conn.execute(query + " ORDER BY at DESC, id DESC", (since, ip) if ip is not None else (since,))
            return [row["at"] for row in rows.fetchall()]

    def signed_in_before(self, ip: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM admin_sign_ins WHERE ok = 1 AND ip = ? LIMIT 1", (ip,)).fetchone() \
                is not None

    def recent_sign_ins(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT at, ip, ok, user_agent FROM admin_sign_ins ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_admin_session(self, *, token_hash: str, expires_at: str, password_tag: str, ip: str,
                          user_agent: str) -> None:
        stamp = now()
        with self.connect() as conn:
            conn.execute("INSERT INTO admin_sessions (token_hash, created_at, expires_at, last_seen_at, password_tag, "
                         "ip, user_agent) VALUES (?,?,?,?,?,?,?)",
                         (token_hash, stamp, expires_at, stamp, password_tag, ip, user_agent))

    def get_admin_session(self, token_hash: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM admin_sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def touch_admin_session(self, token_hash: str, stamp: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?", (stamp, token_hash))

    def delete_admin_session(self, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def delete_admin_sessions_before(self, expired: str, idle: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ? OR last_seen_at <= ?", (expired, idle))

    def delete_sign_ins_before(self, stamp: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM admin_sign_ins WHERE at < ?", (stamp,))

    # --- books the practice adds (see books.py) --------------------------------------------------------------

    def add_book(self, *, filename: str, file_hash: str) -> int:
        stamp = now()
        with self.connect() as conn:
            return conn.execute("INSERT INTO books (created_at, updated_at, filename, file_hash, status) "
                                "VALUES (?,?,?,?, 'reading')", (stamp, stamp, filename, file_hash)).lastrowid

    def get_book(self, book_id: int) -> dict | None:
        with self.connect() as conn:
            return _decode(conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone())

    def list_books(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM books ORDER BY id DESC").fetchall()
        return [_decode(row) for row in rows]

    def book_with_hash(self, file_hash: str) -> dict | None:
        with self.connect() as conn:
            return _decode(conn.execute("SELECT * FROM books WHERE file_hash = ?", (file_hash,)).fetchone())

    def set_book_fields(self, book_id: int, **fields) -> None:
        fields["updated_at"] = now()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE books SET {columns} WHERE id = ?", [*(_encode(v) for v in fields.values()), book_id])

    def move_book(self, book_id: int, from_statuses: tuple[str, ...], to_status: str, **fields) -> bool:
        """Changes the book's status only if it is in one of from_statuses (so two requests cannot both do it)."""
        fields = {"status": to_status, "updated_at": now(), **fields}
        columns = ", ".join(f"{key} = ?" for key in fields)
        marks = ", ".join("?" for _ in from_statuses)
        with self.connect() as conn:
            cursor = conn.execute(f"UPDATE books SET {columns} WHERE id = ? AND status IN ({marks})",
                                  [*(_encode(v) for v in fields.values()), book_id, *from_statuses])
            return cursor.rowcount == 1

    def delete_book(self, book_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM books WHERE id = ?", (book_id,))

    # --- usage accounting -------------------------------------------------

    def add_llm_call(self, *, session_id, purpose, model, stop_reason, usage: dict, request_id) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO llm_calls (created_at, session_id, purpose, model, stop_reason, input_tokens, "
                "output_tokens, cache_creation_input_tokens, cache_read_input_tokens, request_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), session_id, purpose, model, stop_reason,
                 usage.get("input_tokens") or 0, usage.get("output_tokens") or 0,
                 usage.get("cache_creation_input_tokens") or 0, usage.get("cache_read_input_tokens") or 0,
                 request_id),
            )

    def usage_by_session(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT purpose, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(cache_creation_input_tokens) AS cache_write_tokens, "
                "SUM(cache_read_input_tokens) AS cache_read_tokens FROM llm_calls WHERE session_id = ? "
                "GROUP BY purpose, model",
                (session_id,),
            ).fetchall()
        return [_decode(r) for r in rows]
