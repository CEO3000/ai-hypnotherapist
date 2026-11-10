import asyncio

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.llm import (
    FALLBACK_BETA,
    ClaudeGateway,
    LLMRefusal,
    LLMResult,
    LLMUnavailable,
    describe_error,
    usage_totals,
)

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class Usage:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return dict(self.data)


class Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class Message:
    def __init__(self, content, stop_reason="end_turn", parsed_output=None, usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = Usage(usage or {"input_tokens": 10, "output_tokens": 5})
        self.parsed_output = parsed_output
        self._request_id = "req_123"


class FakeStream:
    def __init__(self, final, error=None):
        self.final, self.error = final, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.final


class FakeMessages:
    def __init__(self):
        self.requests = []
        self.stream_result = None
        self.parse_result = None
        self.parse_error = None

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        return self.stream_result

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        if self.parse_error:
            raise self.parse_error
        return self.parse_result


class FakeClient:
    def __init__(self):
        self.beta = Block(messages=FakeMessages())


class Answer(BaseModel):
    value: str


def gateway():
    client = FakeClient()
    return ClaudeGateway("claude-opus-5", client=client), client.beta.messages


def test_complete_returns_text_with_correct_request():
    gw, messages = gateway()
    final = Message([Block(type="thinking", thinking=""), Block(type="text", text="Hello "),
                     Block(type="text", text="there")])
    messages.stream_result = FakeStream(final)
    result = asyncio.run(gw.complete(system="sys", messages=[{"role": "user", "content": "hi"}],
                                     effort="medium", max_tokens=100))
    assert isinstance(result, LLMResult)
    assert result.text == "Hello there"
    assert result.request_id == "req_123"
    request = messages.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "medium"}
    assert "tools" not in request


def test_complete_maps_api_errors_to_unavailable():
    gw, messages = gateway()
    messages.stream_result = FakeStream(None, error=anthropic.APIConnectionError(request=REQUEST))
    with pytest.raises(LLMUnavailable, match="could not connect"):
        asyncio.run(gw.complete(system="s", messages=[], effort="low", max_tokens=10))


def test_complete_raises_on_refusal():
    gw, messages = gateway()
    messages.stream_result = FakeStream(Message([], stop_reason="refusal"))
    with pytest.raises(LLMRefusal):
        asyncio.run(gw.complete(system="s", messages=[], effort="high", max_tokens=10))


def test_parse_returns_validated_output_and_merges_effort():
    gw, messages = gateway()
    messages.parse_result = Message([Block(type="text", text='{"value": "x"}')], parsed_output=Answer(value="x"))
    parsed, result = asyncio.run(gw.parse(system="s", messages=[], output_model=Answer, effort="high",
                                          max_tokens=10))
    assert parsed.value == "x"
    request = messages.requests[0]
    assert request["output_format"] is Answer
    assert request["output_config"] == {"effort": "high"}
    assert request["fallbacks"] == "default"


@pytest.mark.parametrize("message, error", [
    (Message([], stop_reason="refusal"), LLMRefusal),
    (Message([], stop_reason="max_tokens"), LLMUnavailable),
    (Message([], parsed_output=None), LLMUnavailable),
])
def test_parse_rejects_unusable_responses(message, error):
    gw, messages = gateway()
    messages.parse_result = message
    with pytest.raises(error):
        asyncio.run(gw.parse(system="s", messages=[], output_model=Answer, effort="high", max_tokens=10))


def test_parse_maps_authentication_error():
    gw, messages = gateway()
    messages.parse_error = anthropic.AuthenticationError(
        "bad key", response=httpx2.Response(401, request=REQUEST), body=None)
    with pytest.raises(LLMUnavailable, match="API key was rejected"):
        asyncio.run(gw.parse(system="s", messages=[], output_model=Answer, effort="high", max_tokens=10))


def test_missing_credentials_are_reported_as_unavailable(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    gw = ClaudeGateway("claude-opus-5")
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        asyncio.run(gw.parse(system="s", messages=[], output_model=Answer, effort="high", max_tokens=10))
    with pytest.raises(LLMUnavailable, match="ANTHROPIC_API_KEY"):
        asyncio.run(gw.complete(system="s", messages=[], effort="low", max_tokens=10))


def test_usage_totals_sum_fallback_iterations():
    usage = {"input_tokens": 5, "output_tokens": 1, "iterations": [
        {"type": "message", "input_tokens": 100, "output_tokens": 0, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 0},
        {"type": "fallback_message", "input_tokens": 120, "output_tokens": 40, "cache_creation_input_tokens": 10,
         "cache_read_input_tokens": 3},
    ]}
    assert usage_totals(usage) == {"input_tokens": 220, "output_tokens": 40, "cache_creation_input_tokens": 10,
                                   "cache_read_input_tokens": 3}
    assert usage_totals({"input_tokens": 7, "output_tokens": 2})["input_tokens"] == 7


def test_error_descriptions_are_specific():
    rate = anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQUEST), body=None)
    assert "rate limit" in describe_error(rate)
    server = anthropic.InternalServerError("boom", response=httpx2.Response(500, request=REQUEST), body=None)
    assert "HTTP 500" in describe_error(server)
