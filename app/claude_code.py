"""Local test engine: runs the AI through the Claude Code CLI on the account holder's own Claude subscription.

For the subscriber's own testing on this computer only - never for serving other people. While this engine is
active the app refuses any request that does not come from this computer. Production uses ClaudeGateway
(Claude API key), which has the same interface.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic
import pydantic

from .llm import LLMRefusal, LLMResult, LLMUnavailable, LLMUsageLimit

# Never let the CLI fall back to paid API billing: it must use the subscription login.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
USAGE_LIMIT = re.compile(r"usage limit|limit reached|hit your limit|out of (?:extra )?usage|rate.?limit", re.IGNORECASE)


def find_claude_code() -> Path | None:
    on_path = shutil.which("claude")
    if on_path:
        return Path(on_path)
    bundled = sorted(
        (Path.home() / ".vscode" / "extensions").glob("anthropic.claude-code-*/resources/native-binary/claude.exe"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return bundled[0] if bundled else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block.get("type") == "text")


def render_prompt(messages: list[dict]) -> str:
    """Messages API turns -> one prompt. A single user turn is passed through unchanged."""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return _text(messages[0]["content"])
    lines = ["The consultation so far (the client has already read your opening greeting):", ""]
    operator_notes = []
    for message in messages:
        if message["role"] == "system":
            operator_notes.append(_text(message["content"]))
            continue
        speaker = "Client" if message["role"] == "user" else "You (consultation assistant)"
        lines += [f"{speaker}: {_text(message['content'])}", ""]
    for note in operator_notes:
        lines += [f"[Operator instruction: {note}]", ""]
    lines.append("Analyse the client's latest answer, then write your next message to the client.")
    return "\n".join(lines)


class ClaudeCodeGateway:
    engine = "claude-code"

    def __init__(self, exe: Path | None, model: str, workdir: Path, timeout_s: int = 900):
        self.exe = exe
        self.model = model
        self.workdir = workdir
        self.timeout_s = timeout_s

    def _run(self, system_text: str, prompt: str, effort: str, schema: dict | None = None) -> dict:
        if self.exe is None or not self.exe.exists():
            raise LLMUnavailable("Claude Code was not found on this computer (set CLAUDE_CODE_PATH in .env)")
        self.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", dir=self.workdir,
                                         delete=False) as handle:
            handle.write(system_text)
            system_path = handle.name
        args = [str(self.exe), "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
                "--output-format", "json", "--system-prompt-file", system_path, "--effort", effort]
        if self.model:
            args += ["--model", self.model]
        if schema:
            args += ["--json-schema", json.dumps(schema)]
        env = {key: value for key, value in os.environ.items() if key not in STRIPPED_ENV}
        try:
            proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  cwd=self.workdir, env=env, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise LLMUnavailable("Claude Code did not answer in time") from exc
        except OSError as exc:
            raise LLMUnavailable(f"could not start Claude Code: {exc}") from exc
        finally:
            os.unlink(system_path)
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            data = None
        if data is None or proc.returncode != 0 or data.get("is_error"):
            detail = str((data or {}).get("result") or proc.stderr or proc.stdout or "no output").strip()[:300]
            if (data or {}).get("api_error_status") == 429 or USAGE_LIMIT.search(detail):
                raise LLMUsageLimit(f"Claude subscription usage limit reached: {detail}")
            raise LLMUnavailable(f"Claude Code failed (exit {proc.returncode}): {detail}")
        return data

    @staticmethod
    def _model(data: dict) -> str:
        usage = data.get("modelUsage") or {}
        if not usage:
            return "unknown"
        name, entry = max(usage.items(), key=lambda item: item[1].get("costUSD") or 0)
        return entry.get("canonicalModel") or name

    def _result(self, data: dict, text: str) -> LLMResult:
        return LLMResult(text, data.get("stop_reason"), self._model(data), data.get("usage") or {},
                         data.get("session_id"))

    async def complete(self, *, system, messages, effort: str, max_tokens: int) -> LLMResult:
        data = await asyncio.to_thread(self._run, _text(system), render_prompt(messages), effort)
        if data.get("stop_reason") == "refusal":
            raise LLMRefusal("the request was declined")
        return self._result(data, (data.get("result") or "").strip())

    async def parse(self, *, system, messages, output_model: type[pydantic.BaseModel], effort: str,
                    max_tokens: int):
        schema = anthropic.transform_schema(output_model)
        data = await asyncio.to_thread(self._run, _text(system), render_prompt(messages), effort, schema)
        if data.get("stop_reason") == "refusal":
            raise LLMRefusal("the request was declined")
        if data.get("structured_output") is None:
            raise LLMUnavailable("Claude Code returned no structured output")
        try:
            parsed = output_model.model_validate(data["structured_output"])
        except pydantic.ValidationError as exc:
            raise LLMUnavailable(f"structured output did not match the schema: {exc}") from exc
        return parsed, self._result(data, data.get("result") or "")
