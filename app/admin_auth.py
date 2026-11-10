"""Signing in to the admin dashboard. Clients never sign in (they use their private session link); only the
practice's admin email can, with its password and a robot check.

How it is protected:
- The password itself is never stored anywhere: only a slow scrypt hash of it (ADMIN_PASSWORD_HASH in .env, made
  by `python -m app.set_admin_password`, which asks for the password in your own terminal). Guessing a strong
  password from its hash would take far longer than a lifetime.
- The robot check is our own proof of work (the idea behind ALTCHA, with no outside service): the browser must find
  a secret number by trying SHA-256 hashes. That takes a person's device about a second, but makes trying
  passwords by the thousand far too slow. Each check is signed by the server, expires after 10 minutes and works
  only once.
- 5 wrong passwords from one internet address lock that address out for 15 minutes. 20 wrong passwords from
  anywhere pause sign-in for 15 minutes for every address that has never signed in successfully, and the practice
  is emailed about it.
- A wrong email and a wrong password get the same answer, in the same time.
- Signed in, the browser holds a random token in a cookie that page scripts cannot read (HttpOnly), that other
  sites cannot send (SameSite=Strict) and that only travels over https once the site has it (Secure). Only the
  token's hash is stored. It ends after 12 hours, after 2 hours unused, on signing out, or when the password
  changes. Every change made in the dashboard must also carry a header other sites cannot add.
"""

import base64
import hashlib
import hmac
import math
import secrets
import time
from datetime import datetime, timedelta, timezone

COOKIE = "admin_session"
REQUEST_HEADER = ("x-requested-with", "admin")  # sent by the dashboard's own scripts with every change
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1  # about 32 MB of memory and a tenth of a second per check
MIN_PASSWORD_LENGTH = 12
ADDRESS_LIMIT, OVERALL_LIMIT = 5, 20  # wrong passwords within LOCK_MINUTES
LOCK_MINUTES = 15
SESSION_HOURS, IDLE_HOURS = 12, 2
ROBOT_CHECK_SECONDS = 600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    """scrypt:N:r:p:salt:hash (no "$", so no environment file or host ever reads part of it as a variable)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, maxmem=2**26, dklen=32)
    return f"scrypt:{n}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split(":")
        expected = _unb64(digest)
        if kind != "scrypt" or not expected:
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                   maxmem=2**26, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def password_problems(password: str, email: str = "") -> list[str]:
    """What makes a new admin password too weak (empty when it is strong enough)."""
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    kinds = sum(any(test(c) for c in password) for test in (str.islower, str.isupper, str.isdigit,
                                                            lambda c: not c.isalnum()))
    if len(password) < 20 and kinds < 3:
        problems.append("Mix at least three of: small letters, capital letters, numbers, symbols (or use a "
                        "passphrase of 20 characters or more).")
    lowered = password.lower()
    name = email.split("@")[0].lower()
    if (len(name) >= 3 and name in lowered) or any(word in lowered for word in ("password", "hypno", "admin", "1234",
                                                                                  "qwerty")):
        problems.append("Don't use your name, the practice's name or common words like password, admin or 1234.")
    if len(set(password)) < 6:
        problems.append("Use more different characters.")
    return problems


class RobotCheck:
    """The proof-of-work robot check (see the module notes)."""

    def __init__(self, max_number: int):
        self.max_number = max_number
        self.key = secrets.token_bytes(32)  # a restart simply makes open checks start again
        self.used: dict[str, float] = {}

    def _sign(self, challenge: str) -> str:
        return hmac.new(self.key, challenge.encode(), hashlib.sha256).hexdigest()

    def challenge(self) -> dict:
        salt = f"{secrets.token_hex(12)}.{int(time.time()) + ROBOT_CHECK_SECONDS}"
        number = secrets.randbelow(self.max_number + 1)
        challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
        return {"algorithm": "SHA-256", "challenge": challenge, "salt": salt, "maxnumber": self.max_number,
                "signature": self._sign(challenge)}

    def verify(self, solution: dict | None) -> bool:
        """True once for a solved, unexpired check this server issued."""
        if not isinstance(solution, dict):
            return False
        try:
            salt, challenge = str(solution["salt"]), str(solution["challenge"])
            number, signature = int(solution["number"]), str(solution["signature"])
            expires = int(salt.rsplit(".", 1)[1])
        except (KeyError, ValueError, TypeError, IndexError):
            return False
        now = time.time()
        self.used = {c: t for c, t in self.used.items() if t > now}
        if expires < now or challenge in self.used or not 0 <= number <= self.max_number:
            return False
        if not hmac.compare_digest(self._sign(challenge), signature):
            return False
        if not hmac.compare_digest(hashlib.sha256(f"{salt}{number}".encode()).hexdigest(), challenge):
            return False
        self.used[challenge] = expires
        return True


class SignInRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AdminAuth:
    def __init__(self, settings, db, alert=None):
        self.settings = settings
        self.db = db
        self.robot = RobotCheck(settings.robot_check_max_number)
        self.alert = alert  # called with the number of wrong passwords when sign-in is paused for everyone

    @property
    def enabled(self) -> bool:
        return bool(self.settings.admin_password_hash and self.settings.admin_email)

    def _password_tag(self) -> str:
        return hashlib.sha256(self.settings.admin_password_hash.encode()).hexdigest()[:16]

    def _minutes_left(self, failures: list[str], limit: int, now: datetime) -> int:
        """Minutes until fewer than `limit` of these failures (newest first) are within the lock window."""
        if len(failures) < limit:
            return 0
        ends = datetime.fromisoformat(failures[limit - 1]) + timedelta(minutes=LOCK_MINUTES)
        return max(1, math.ceil((ends - now).total_seconds() / 60))

    def locked_minutes(self, ip: str, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        since = _utc(now - timedelta(minutes=LOCK_MINUTES))
        minutes = self._minutes_left(self.db.sign_in_failures(since=since, ip=ip), ADDRESS_LIMIT, now)
        if not minutes and not self.db.signed_in_before(ip):
            minutes = self._minutes_left(self.db.sign_in_failures(since=since), OVERALL_LIMIT, now)
        return minutes

    def sign_in(self, *, email: str, password: str, robot: dict | None, ip: str, user_agent: str) -> str:
        """The new session's token (for the cookie), or SignInRefused."""
        if not self.enabled:
            raise SignInRefused(503, "Admin sign-in is not set up yet. On the server, run: "
                                     "python -m app.set_admin_password")
        minutes = self.locked_minutes(ip)
        if minutes:
            raise SignInRefused(429, f"Too many attempts. Please try again in {minutes} minutes.")
        if not self.robot.verify(robot):
            raise SignInRefused(400, "Please tick the robot check again.")
        email_ok = hmac.compare_digest(email.strip().lower().encode(), self.settings.admin_email.encode())
        password_ok = check_password(password, self.settings.admin_password_hash)  # checked either way: same time
        if not (email_ok and password_ok):
            self.db.add_sign_in(ip=ip, ok=False, user_agent=user_agent[:300])
            now = datetime.now(timezone.utc)
            failures = self.db.sign_in_failures(since=_utc(now - timedelta(minutes=LOCK_MINUTES)))
            if len(failures) == OVERALL_LIMIT and self.alert:
                self.alert(OVERALL_LIMIT)
            minutes = self.locked_minutes(ip, now)
            if minutes:
                raise SignInRefused(429, f"The email or password is not right. Too many attempts: please try again "
                                         f"in {minutes} minutes.")
            raise SignInRefused(401, "The email or password is not right.")
        self.db.add_sign_in(ip=ip, ok=True, user_agent=user_agent[:300])
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        self.db.add_admin_session(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                                  expires_at=_utc(now + timedelta(hours=SESSION_HOURS)),
                                  password_tag=self._password_tag(), ip=ip, user_agent=user_agent[:300])
        return token

    def session(self, token: str) -> dict | None:
        """The signed-in session this cookie token belongs to, while it is still valid."""
        if not token or not self.enabled:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.get_admin_session(token_hash)
        now = datetime.now(timezone.utc)
        if row is None or row["expires_at"] <= _utc(now) or row["password_tag"] != self._password_tag() \
                or row["last_seen_at"] <= _utc(now - timedelta(hours=IDLE_HOURS)):
            if row is not None:
                self.db.delete_admin_session(token_hash)
            return None
        if row["last_seen_at"] <= _utc(now - timedelta(minutes=1)):
            self.db.touch_admin_session(token_hash, _utc(now))
        return row

    def sign_out(self, token: str) -> None:
        if token:
            self.db.delete_admin_session(hashlib.sha256(token.encode()).hexdigest())

    def cleanup(self) -> None:
        """Hourly: ended sessions are removed; the sign-in record is kept for 90 days."""
        now = datetime.now(timezone.utc)
        self.db.delete_admin_sessions_before(_utc(now), _utc(now - timedelta(hours=IDLE_HOURS)))
        self.db.delete_sign_ins_before(_utc(now - timedelta(days=90)))
