"""Production AI engine: the only module that talks to the Claude API."""

import logging
from dataclasses import dataclass

import anthropic
import pydantic

log = logging.getLogger(__name__)

# Server-side refusal fallback: if the primary model's safety classifiers decline a request,
# Anthropic re-runs it on its recommended fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class LLMUnavailable(RuntimeError):
    """The AI service could not produce a usable answer."""


class LLMUsageLimit(LLMUnavailable):
    """The Claude subscription behind the test engine has reached its usage limit for now."""


class LLMRefusal(RuntimeError):
    """Every model in the fallback chain declined the request."""


@dataclass
class LLMResult:
    text: str
    stop_reason: str | None
    model: str
    usage: dict
    request_id: str | None


def describe_error(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "the Claude API key was rejected (check ANTHROPIC_API_KEY)"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "the Claude API key lacks permission for this request"
    if isinstance(exc, anthropic.NotFoundError):
        return "the configured Claude model or endpoint was not found (check CLAUDE_MODEL)"
    if isinstance(exc, anthropic.BadRequestError):
        return f"the Claude API rejected the request: {exc.message}"
    if isinstance(exc, anthropic.RateLimitError):
        return "the Claude API rate limit was reached"
    if isinstance(exc, anthropic.APIStatusError):
        return f"the Claude API returned HTTP {exc.status_code}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not connect to the Claude API"
    return f"unexpected Claude API error: {exc}"


def usage_totals(usage: dict) -> dict:
    """Billable token totals, summed over every attempt when a fallback model was involved."""
    iterations = usage.get("iterations") or []
    source = iterations if iterations else [usage]
    return {field: sum((entry.get(field) or 0) for entry in source) for field in TOKEN_FIELDS}


class ClaudeGateway:
    engine = "api"

    def __init__(self, model: str, client: anthropic.AsyncAnthropic | None = None, plain: bool = False):
        self.model = model
        self._client = client
        self.plain = plain  # a smaller model: no adaptive thinking, effort setting or fallback chain

    def with_model(self, model: str) -> "ClaudeGateway":
        """The same API key with another model, sent plain requests (for routine work such as reading books,
        where a smaller, much cheaper model does the job)."""
        if model == self.model:
            return self
        return ClaudeGateway(model, self._client, plain=True)

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            try:
                client = anthropic.AsyncAnthropic()
            except anthropic.AnthropicError as exc:
                raise LLMUnavailable(f"Claude API is not configured: {exc}") from exc
            # Without credentials the SDK only fails at request time, with a TypeError.
            if client.api_key is None and client.auth_token is None and client.credentials is None:
                raise LLMUnavailable("no Claude API credentials found (set ANTHROPIC_API_KEY in .env)")
            self._client = client
        return self._client

    def _request(self, *, system, messages, effort: str, max_tokens: int) -> dict:
        if self.plain:
            return {"model": self.model, "max_tokens": max_tokens, "system": system, "messages": messages}
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }

    @staticmethod
    def _result(message) -> LLMResult:
        return LLMResult(
            text="".join(block.text for block in message.content if block.type == "text"),
            stop_reason=message.stop_reason,
            model=message.model,
            usage=message.usage.to_dict() if message.usage else {},
            request_id=getattr(message, "_request_id", None),
        )

    async def complete(self, *, system, messages, effort: str, max_tokens: int) -> LLMResult:
        """Long single-shot generation, streamed under the hood to avoid HTTP timeouts."""
        request = self._request(system=system, messages=messages, effort=effort, max_tokens=max_tokens)
        try:
            async with self.client.beta.messages.stream(**request) as stream:
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            log.error("Claude request failed: %s", describe_error(exc))
            raise LLMUnavailable(describe_error(exc)) from exc
        if final.stop_reason == "refusal":
            raise LLMRefusal("the request was declined by every model in the fallback chain")
        return self._result(final)

    async def parse(self, *, system, messages, output_model: type[pydantic.BaseModel], effort: str,
                    max_tokens: int):
        """Structured output validated against output_model. Returns (parsed, LLMResult)."""
        request = self._request(system=system, messages=messages, effort=effort, max_tokens=max_tokens)
        try:
            response = await self.client.beta.messages.parse(output_format=output_model, **request)
        except anthropic.AnthropicError as exc:
            log.error("Claude parse failed: %s", describe_error(exc))
            raise LLMUnavailable(describe_error(exc)) from exc
        except pydantic.ValidationError as exc:
            raise LLMUnavailable(f"structured output did not match the schema: {exc}") from exc
        result = self._result(response)
        if response.stop_reason == "refusal":
            raise LLMRefusal("the request was declined by every model in the fallback chain")
        if response.stop_reason == "max_tokens":
            raise LLMUnavailable("structured output was cut off by max_tokens")
        if response.parsed_output is None:
            raise LLMUnavailable("the response contained no structured output")
        return response.parsed_output, result
