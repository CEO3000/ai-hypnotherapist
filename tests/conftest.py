import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.admin_auth import COOKIE, hash_password
from app.config import load_settings
from app.main import create_app
from tests.support import FakeGateway, make_knowledge_base

try:  # the HTTP library Starlette's test client uses
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Calm-harbour-lantern-47"
ADMIN_PASSWORD_HASH = hash_password(ADMIN_PASSWORD, n=2**10)  # a quick hash for tests (the real one is far slower)
ADMIN_HEADER = {"X-Requested-With": "admin"}


def solve_robot_check(check: dict) -> dict:
    """What the sign-in page does in the browser: find the number whose SHA-256 hash the server asked for."""
    for number in range(check["maxnumber"] + 1):
        if hashlib.sha256(f"{check['salt']}{number}".encode()).hexdigest() == check["challenge"]:
            return {**check, "number": number}
    raise AssertionError("the robot check has no solution")


class SignedIn(httpx.Auth):
    """Requests as the signed-in admin: signs in as the sign-in page does (robot check included) whenever the app
    asks, and sends the dashboard's own header with every request."""

    requires_response_body = True

    def auth_flow(self, request):
        request.headers.update(ADMIN_HEADER)
        response = yield request
        if response.status_code != 401:
            return
        check = (yield httpx.Request("GET", request.url.join("/api/admin/robot-check"))).json()
        signed = yield httpx.Request("POST", request.url.join("/api/admin/sign-in"), headers=ADMIN_HEADER, json={
            "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "robot": solve_robot_check(check)})
        assert signed.status_code == 200, signed.text
        request.headers["Cookie"] = f"{COOKIE}={signed.cookies[COOKIE]}"
        yield request


CLINICIAN_AUTH = SignedIn()


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls the real Claude API (costs money); needs RUN_LIVE_TESTS=1")
    config.addinivalue_line("markers", "real_ffmpeg: mixes session audio with the real ffmpeg")


@pytest.fixture(autouse=True)
def quick_session_audio(request, monkeypatch):
    """Delivered sessions' audio files are 'mixed' instantly (a few bytes) unless a test asks for real ffmpeg."""
    if request.node.get_closest_marker("real_ffmpeg"):
        return

    def mix(ffmpeg, voice_pcm, seconds, out, **_):
        out.write_bytes(b"ID3" + bytes(61))

    monkeypatch.setattr("app.session_audio.mix", mix)
    monkeypatch.setattr("app.session_audio.find_ffmpeg", lambda: "ffmpeg")


@pytest.fixture
def kb_root(tmp_path):
    return make_knowledge_base(tmp_path / "kb")


@pytest.fixture
def settings(tmp_path, kb_root):
    return replace(
        load_settings(),
        knowledge_root=kb_root,
        data_dir=tmp_path / "data",
        admin_email=ADMIN_EMAIL,
        admin_password_hash=ADMIN_PASSWORD_HASH,
        robot_check_max_number=50,
        languages=("en", "de", "fr", "es"),  # whatever LANGUAGES says in this computer's .env
        require_clinician_review=True,
        soft_turn_limit=22,
        hard_turn_limit=30,
        consultation_min_answers=1,  # most tests finish a consultation in a couple of answers
        offer_ai_when_unsuitable=False,
        smtp_host="",  # tests never send real emails: they are saved to the test's own outbox
        smtp_password="",
        payment_provider="test",  # never real Stripe calls from tests, whatever .env says
        stripe_secret_key="",
        stripe_webhook_secret="",
        embed_origins=(),  # nor the .env's website settings
        client_page_url="",
    )


@pytest.fixture
def fake():
    return FakeGateway()


@pytest.fixture
def make_client(settings, fake):
    clients = []

    def build(**overrides):
        client = TestClient(create_app(replace(settings, **overrides), gateway=fake))
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()
