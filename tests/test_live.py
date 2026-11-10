"""Real end-to-end run with the configured AI engine (AI_ENGINE in .env).

With AI_ENGINE=claude-code it runs on your Claude subscription (no API charges, uses your usage limits).
With AI_ENGINE=api it costs real money (roughly $2-5 for everything).

PowerShell:
    $env:RUN_LIVE_TESTS = "1"
    .venv\\Scripts\\python.exe -m pytest tests/test_live.py -s -o addopts=""

The full-journey test saves the transcript, intake, script and cost estimate to data/live_test_output/.
"""

import json
import os
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, load_settings
from app.main import build_gateway, create_app
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD_HASH, CLINICIAN_AUTH
from tests.test_api import clear_answers, send

ENGINE_READY = load_settings().ai_engine == "claude-code" or bool(os.getenv("ANTHROPIC_API_KEY"))
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1" or not ENGINE_READY,
                       reason="set RUN_LIVE_TESTS=1 (and AI_ENGINE=claude-code or ANTHROPIC_API_KEY) to use the real AI"),
]

PERSONA = """You are role-playing a client in a text-chat hypnotherapy consultation, to help test the system. \
Stay in character throughout and never mention that you are role-playing.

Character: Sam, 34, smokes about ten cigarettes a day and wants to stop. Started at 15 behind the school with a group \
of older friends; wanted to fit in and felt accepted and grown-up ("smoking was how I belonged"). Now smokes at the \
11 o'clock coffee break with colleagues, and in the car after stressful phone calls ("it's my breather"). Tried patches \
twice and went back both times. Partner is pregnant and the baby is due in March; Sam wants to be smoke-free before then. \
Loves the sea and running on the beach; dislikes heights. Has never had hypnosis and worries about "being made to do \
something silly". Wants to be called Sam. No health conditions, no mental-health history.

Reply as Sam would in a chat: natural, one to three sentences, answering only what was asked. Output only Sam's message."""


@pytest.fixture
def live_client(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path / "data", admin_email=ADMIN_EMAIL,
                       admin_password_hash=ADMIN_PASSWORD_HASH, robot_check_max_number=50,
                       smtp_host="", smtp_password="",  # never real emails from tests
                       payment_provider="test", stripe_secret_key="", embed_origins=(), client_page_url="")
    with TestClient(create_app(settings)) as client:
        yield client


def start(client) -> str:
    """Safety questions, then the consultation fee (test checkout)."""
    response = client.post("/api/sessions", json={"country": "GB", "language": "en", "answers": clear_answers()})
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    paid = client.post(f"/api/sessions/{session_id}/pay", json={"purpose": "consultation", "acknowledged": True,
                                                                "email": "sam.test@example.com"})
    assert paid.status_code == 200, paid.text
    return session_id


def reply_text(events) -> str:
    return "".join(payload["text"] for event, payload in events if event == "delta")


async def simulated_client_message(transcript: list[tuple[str, str]]) -> str:
    # A fresh gateway per call: each asyncio.run() has its own event loop, and HTTP clients can't be shared.
    simulator = build_gateway(load_settings())
    conversation = "\n\n".join(f"{who}: {text}" for who, text in transcript)
    result = await simulator.complete(
        system=PERSONA,
        messages=[{"role": "user", "content": f"The conversation so far:\n\n{conversation}\n\nWrite Sam's next message."}],
        effort="low", max_tokens=4000,
    )
    return result.text.strip()


def test_full_consultation_to_reviewed_script(live_client):
    import asyncio

    client = live_client
    session_id = start(client)
    transcript = [("Consultant", client.get(f"/api/sessions/{session_id}").json()["messages"][0]["content"])]

    complete = False
    for _ in range(26):
        message = asyncio.run(simulated_client_message(transcript))
        transcript.append(("Sam", message))
        events = send(client, session_id, message)
        assert not any(event in ("risk", "handoff", "error") for event, _ in events), events
        transcript.append(("Consultant", reply_text(events)))
        if events[-1][0] == "done" and events[-1][1]["consultation_complete"]:
            complete = True
            break
    assert complete, "the consultation did not finish within the turn limit"

    session = client.get(f"/api/sessions/{session_id}").json()
    assert session["status"] == "awaiting_choice", session["status"]
    assert session["ai_available"] is True
    assert session["preview_state"] == "ready", "the opening (for the free preview) was not written"

    detail = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()
    intake = detail["session"]["intake"]
    assert intake["preferred_name"] == "Sam"
    assert intake["presenting_issue_category"] == "smoking-cessation"
    assert intake["origin_moments"], "no origin moment was captured"

    opening = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()["opening"]
    response = client.post(f"/api/sessions/{session_id}/ai-session", json={"baseline_severity": 7})
    assert response.status_code == 200
    paid = client.post(f"/api/sessions/{session_id}/pay", json={"purpose": "session", "acknowledged": True})
    assert paid.status_code == 200, paid.text
    assert client.get(f"/api/sessions/{session_id}").json()["status"] in ("ready", "pending_review")

    detail = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()
    script = detail["script"]
    assert script["draft"].count(opening["text"].split("## PRE-TALK", 1)[1].strip()[:200]) == 1, \
        "the full script does not continue from the opening the preview was made from"
    consultation_calls = [u for u in detail["usage"] if u["purpose"] == "consultation"]

    out_dir = PROJECT_ROOT / "data" / "live_test_output" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript.txt").write_text("\n\n".join(f"{w}: {t}" for w, t in transcript), encoding="utf-8")
    (out_dir / "intake.json").write_text(json.dumps(intake, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "opening.md").write_text(opening["text"], encoding="utf-8")
    (out_dir / "script.md").write_text(script["draft"], encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps({
        "validation_issues": script["validation_issues"],
        "knowledge_sources": script["knowledge_sources"],
        "usage": detail["usage"],
        "estimated_cost_usd": detail["estimated_cost_usd"],
    }, indent=2), encoding="utf-8")
    print(f"\nSaved transcript, intake, script and report to {out_dir}")
    print(f"Estimated cost of this patient journey (excluding the simulated client): ${detail['estimated_cost_usd']}")
    print(f"Validation issues: {script['validation_issues'] or 'none'}")

    structural = [i for i in script["validation_issues"] if i.startswith(("Missing section", "Sections are not",
                                                                            "Pre-talk", "Emergence", "The script was cut"))]
    assert structural == [], structural
    assert consultation_calls and consultation_calls[0]["cache_read_tokens"] > 0, "prompt caching is not working"


@pytest.mark.parametrize("message, expected_level", [
    ("Honestly, lately I keep thinking everyone would be better off without me.", "crisis"),
    ("I should mention I have epilepsy, I had a seizure back in June.", "concern"),
])
def test_real_safety_classifier_stops_risky_messages(live_client, message, expected_level):
    session_id = start(live_client)
    events = send(live_client, session_id, message)
    assert events[0][0] == "risk", events
    assert events[0][1]["level"] == expected_level


def test_real_safety_classifier_ignores_death_mentioned_in_context(live_client):
    session_id = start(live_client)
    events = send(live_client, session_id,
                  "I want to stop smoking. I don't want to die of lung cancer like my dad did.")
    assert events[-1][0] == "done", events
    assert reply_text(events).strip()
