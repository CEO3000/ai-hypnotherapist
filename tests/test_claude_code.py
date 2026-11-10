import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import claude_code
from app.claude_code import ClaudeCodeGateway, render_prompt
from app.consultation import ConsultantTurn
from app.llm import LLMRefusal, LLMResult, LLMUnavailable, LLMUsageLimit
from app.main import create_app
from app.risk import RiskAssessment

USAGE = {"input_tokens": 3, "output_tokens": 50, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 0}


class FakeCLI:
    """Stands in for subprocess.run and records how Claude Code would have been called."""

    def __init__(self, payload: dict | None = None, returncode: int = 0, stdout: str | None = None):
        self.payload = payload or {}
        self.returncode = returncode
        self.stdout = stdout
        self.calls = []

    def __call__(self, args, **kwargs):
        system_path = Path(args[args.index("--system-prompt-file") + 1])
        self.calls.append({"args": args, "system": system_path.read_text(encoding="utf-8"), **kwargs})
        body = {"is_error": False, "stop_reason": "end_turn", "usage": USAGE, "session_id": "s1",
                "modelUsage": {"claude-opus-5-5": {"costUSD": 0.01, "canonicalModel": "claude-opus-5-5"},
                               "claude-haiku-4-5-20251001": {"costUSD": 0.0001}},
                **self.payload}
        return subprocess.CompletedProcess(args, self.returncode, self.stdout or json.dumps(body), "boom")


@pytest.fixture
def gateway(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("")
    return ClaudeCodeGateway(exe, "opus", tmp_path / "work")


def use_cli(monkeypatch, fake: FakeCLI) -> FakeCLI:
    monkeypatch.setattr(claude_code.subprocess, "run", fake)
    return fake


def test_single_prompt_passes_through_and_conversation_is_rendered():
    assert render_prompt([{"role": "user", "content": "hello"}]) == "hello"
    text = render_prompt([
        {"role": "user", "content": "I want to stop smoking"},
        {"role": "assistant", "content": "When did it start?"},
        {"role": "user", "content": [{"type": "text", "text": "At fifteen", "cache_control": {"type": "ephemeral"}}]},
        {"role": "system", "content": "Please wrap up now."},
    ])
    assert "Client: I want to stop smoking" in text
    assert "You (consultation assistant): When did it start?" in text
    assert "Client: At fifteen" in text
    assert "[Operator instruction: Please wrap up now.]" in text
    assert text.endswith("Analyse the client's latest answer, then write your next message to the client.")


def test_command_is_isolated_and_never_uses_paid_api_keys(gateway, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-should-not-be-used")
    fake = use_cli(monkeypatch, FakeCLI({"result": "Hello"}))
    result = asyncio.run(gateway.complete(system=[{"type": "text", "text": "SYSTEM TEXT"}],
                                          messages=[{"role": "user", "content": "£30 · can’t wait"}],
                                          effort="high", max_tokens=100))
    call = fake.calls[0]
    for flag in ("-p", "--safe-mode", "--no-session-persistence"):
        assert flag in call["args"]
    assert call["args"][call["args"].index("--tools") + 1] == ""
    assert call["args"][call["args"].index("--effort") + 1] == "high"
    assert call["args"][call["args"].index("--model") + 1] == "opus"
    assert "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert call["input"] == "£30 · can’t wait" and call["encoding"] == "utf-8"
    assert call["system"] == "SYSTEM TEXT"
    assert not list((gateway.workdir).glob("*.txt"))  # the temporary instructions file is removed
    assert isinstance(result, LLMResult)
    assert result.text == "Hello" and result.model == "claude-opus-5-5" and result.usage == USAGE


def test_consultant_turn_is_parsed_from_a_rendered_conversation(gateway, monkeypatch):
    structured = {
        "analysis": {"facts": ["started at 15"], "feelings": ["wanted to fit in"], "beliefs_or_conclusions": [],
                     "links_to_earlier_answers": [], "inconsistencies": [], "gaps": ["what happened next"],
                     "next_focus": "the first time behind the school"},
        "delivery": "gentle",
        "reply": "Thank you. What happened that first time?", "consultation_complete": False, "coverage_note": "",
    }
    fake = use_cli(monkeypatch, FakeCLI({"result": "{}", "structured_output": structured}))
    turn, _ = asyncio.run(gateway.parse(
        system="S", output_model=ConsultantTurn, effort="high", max_tokens=100,
        messages=[{"role": "user", "content": "I want to stop smoking"},
                  {"role": "assistant", "content": "When did it start?"},
                  {"role": "user", "content": "At 15, to fit in"}]))
    assert turn.reply == "Thank you. What happened that first time?"
    assert turn.analysis.next_focus == "the first time behind the school"
    assert "Client: At 15, to fit in" in fake.calls[0]["input"]
    schema = json.loads(fake.calls[0]["args"][fake.calls[0]["args"].index("--json-schema") + 1])
    assert list(schema["properties"])[:3] == ["analysis", "delivery", "reply"]  # analysis comes before the reply


def test_parse_uses_a_strict_schema_and_validates(gateway, monkeypatch):
    fake = use_cli(monkeypatch, FakeCLI({"result": "{}", "structured_output": {
        "level": "none", "categories": [], "evidence": "", "rationale": "Fine."}}))
    parsed, _ = asyncio.run(gateway.parse(system="S", messages=[{"role": "user", "content": "x"}],
                                          output_model=RiskAssessment, effort="medium", max_tokens=100))
    assert parsed.level == "none"
    schema = json.loads(fake.calls[0]["args"][fake.calls[0]["args"].index("--json-schema") + 1])
    assert schema["additionalProperties"] is False and "level" in schema["required"]


@pytest.mark.parametrize("fake, error", [
    (FakeCLI(returncode=1, stdout=" "), LLMUnavailable),
    (FakeCLI(stdout="not json"), LLMUnavailable),
    (FakeCLI({"is_error": True, "result": "Usage limit reached"}), LLMUnavailable),
    (FakeCLI({"result": "{}"}), LLMUnavailable),
    (FakeCLI({"result": "{}", "structured_output": {"level": "unknown"}}), LLMUnavailable),
    (FakeCLI({"stop_reason": "refusal", "result": ""}), LLMRefusal),
])
def test_parse_failures_become_safe_errors(gateway, monkeypatch, fake, error):
    use_cli(monkeypatch, fake)
    with pytest.raises(error):
        asyncio.run(gateway.parse(system="S", messages=[{"role": "user", "content": "x"}],
                                  output_model=RiskAssessment, effort="medium", max_tokens=100))


@pytest.mark.parametrize("fake", [
    FakeCLI({"is_error": True, "result": "Claude AI usage limit reached|1790380800"}, returncode=1),
    FakeCLI({"is_error": True, "result": "You've hit your limit · resets 3am"}),
    FakeCLI({"is_error": True, "api_error_status": 429, "result": "API Error"}),
])
def test_usage_limit_is_reported_as_such(gateway, monkeypatch, fake):
    use_cli(monkeypatch, fake)
    with pytest.raises(LLMUsageLimit):
        asyncio.run(gateway.complete(system="S", messages=[{"role": "user", "content": "x"}], effort="low",
                                     max_tokens=10))


def test_usage_limit_reaches_the_client_as_a_clear_error(settings, gateway, monkeypatch):
    use_cli(monkeypatch, FakeCLI({"is_error": True, "result": "Claude AI usage limit reached"}, returncode=1))
    from tests.test_api import send, start
    with TestClient(create_app(replace(settings, ai_engine="claude-code"), gateway=gateway)) as client:
        session_id = start(client)["session_id"]
        assert send(client, session_id, "Can you hear me?") == [("error", {"code": "usage_limit"})]


def test_missing_claude_code_is_reported(tmp_path):
    missing = ClaudeCodeGateway(None, "opus", tmp_path)
    with pytest.raises(LLMUnavailable, match="CLAUDE_CODE_PATH"):
        asyncio.run(missing.complete(system="S", messages=[{"role": "user", "content": "x"}], effort="low",
                                     max_tokens=10))


def test_timeout_is_reported(gateway, monkeypatch):
    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(claude_code.subprocess, "run", slow)
    with pytest.raises(LLMUnavailable, match="in time"):
        asyncio.run(gateway.complete(system="S", messages=[{"role": "user", "content": "x"}], effort="low",
                                     max_tokens=10))


def test_test_engine_refuses_other_computers_and_labels_itself(settings, gateway):
    app = create_app(replace(settings, ai_engine="claude-code"), gateway=gateway)
    with TestClient(app) as local:
        config = local.get("/api/config")
        assert config.status_code == 200 and config.json()["engine"] == "claude-code"
    with TestClient(app, client=("192.168.1.20", 5555)) as remote:
        assert remote.get("/api/config").status_code == 403
        assert remote.get("/").status_code == 403


def test_api_engine_accepts_other_computers(settings, fake):
    with TestClient(create_app(settings, gateway=fake), client=("192.168.1.20", 5555)) as remote:
        assert remote.get("/api/config").json()["engine"] == "api"
