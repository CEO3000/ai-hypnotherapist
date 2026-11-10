"""Sets the admin dashboard's password. Double-click set-admin-password.bat in the project folder, or run:

    .venv\\Scripts\\python -m app.set_admin_password

It asks for the new password twice (a * shows for each character typed, never the character itself), checks that
it is strong, and saves only its hash to .env as ADMIN_PASSWORD_HASH: the password itself is never stored. Restart
the server afterwards; anyone signed in with the old password is signed out."""

import getpass
import sys

from .admin_auth import hash_password, password_problems
from .config import PROJECT_ROOT, load_settings

KEY = "ADMIN_PASSWORD_HASH"


def ask(prompt: str) -> str:
    """Reads a password, showing a * for each character so it is clear the typing works (Windows console;
    elsewhere nothing shows, as usual)."""
    try:
        import msvcrt
    except ImportError:
        return getpass.getpass(prompt)
    print(prompt, end="", flush=True)
    typed: list[str] = []
    while True:
        char = msvcrt.getwch()
        if char in ("\r", "\n"):
            print()
            return "".join(typed)
        if char == "\x03":  # Ctrl+C
            print()
            raise KeyboardInterrupt
        if char in ("\x00", "\xe0"):  # arrow and function keys send two codes: ignored
            msvcrt.getwch()
            continue
        if char == "\x08":  # Backspace
            if typed:
                typed.pop()
                print("\b \b", end="", flush=True)
            continue
        typed.append(char)
        print("*", end="", flush=True)


def main() -> int:
    email = load_settings().admin_email
    print(f"Admin email: {email}  (to use another, set ADMIN_EMAIL in .env)")
    print("Type your new password and press Enter. A * appears for each character you type.")
    password = ask("New admin password: ")
    problems = password_problems(password, email)
    if problems:
        print("That password is not strong enough, so nothing was changed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    if ask("Type it again: ") != password:
        print("The two passwords were different, so nothing was changed.")
        return 1
    env = PROJECT_ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if not line.strip().startswith(f"{KEY}=")]
    lines.append(f"{KEY}={hash_password(password)}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Saved (only a hash of the password is in .env). Restart the app (start.bat) to use it.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Stopped: nothing was changed.")
        sys.exit(1)
