"""LLM backends behind one interface: exactly one tool call per turn.

  AnthropicLLM   - Claude via the Anthropic Messages API (tool use, forced tool choice, prompt caching,
                   per-call timeout from the run deadline, fallback model on overload/timeout)
  OpenAICompatLLM - any OpenAI-compatible chat-completions endpoint (OpenAI, or local Ollama)
  DeterministicReasoner (scripted.py) - no key needed; same tools, same guardrails

Conversation is kept provider-neutral:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_call": {"id", "name", "args"}}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..models import RunState

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
FALLBACK_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


class LLMUnavailable(Exception):
    pass


@dataclass
class LLMResponse:
    tool_name: str
    tool_args: dict
    tool_id: str
    thought: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    latency_ms: float = 0.0


class LLM(Protocol):
    name: str
    model: str

    def next(self, system: str, messages: list[dict], tools: list[dict], run: RunState, phase: str,
             timeout_s: float) -> LLMResponse: ...


# ---------------------------------------------------------------------------- Anthropic
class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str | None = None, fallback: str | None = FALLBACK_ANTHROPIC_MODEL) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=2)
        self.model = model or os.environ.get("ASAP_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self.fallback = os.environ.get("ASAP_FALLBACK_MODEL", fallback or "") or None

    @staticmethod
    def to_messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                role, blocks = "user", [{"type": "text", "text": m["text"]}]
            elif m["role"] == "assistant":
                role, blocks = "assistant", []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                tc = m["tool_call"]
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
            else:
                role, blocks = "user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                         "content": m["content"], "is_error": bool(m.get("is_error"))}]
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})
        return out

    def _call(self, model: str, system: str, messages: list[dict], tools: list[dict], timeout_s: float):  # type: ignore[no-untyped-def]
        tool_defs = [dict(t) for t in tools]
        tool_defs[-1] = {**tool_defs[-1], "cache_control": {"type": "ephemeral"}}
        return self.client.messages.create(
            model=model, max_tokens=2048,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tool_defs, tool_choice={"type": "any", "disable_parallel_tool_use": True},
            messages=self.to_messages(messages), timeout=timeout_s)

    def next(self, system: str, messages: list[dict], tools: list[dict], run: RunState, phase: str,
             timeout_s: float) -> LLMResponse:
        a = self._anthropic
        t0 = time.time()
        models = [self.model] + ([self.fallback] if self.fallback and self.fallback != self.model else [])
        last_err: Exception | None = None
        for model in models:
            try:
                resp = self._call(model, system, messages, tools, timeout_s)
                break
            except (a.APITimeoutError, a.RateLimitError, a.InternalServerError, a.APIConnectionError) as e:
                last_err = e  # hedge to the fallback model
            except a.APIStatusError as e:
                if e.status_code in (429, 500, 502, 503, 529):
                    last_err = e
                    continue
                raise LLMUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
        else:
            raise LLMUnavailable(f"LLM unavailable after fallback: {last_err}")
        tool = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool is None:
            raise LLMUnavailable("model returned no tool call despite forced tool choice")
        thought = " ".join(b.text for b in resp.content if b.type == "text").strip()
        return LLMResponse(tool.name, dict(tool.input), tool.id, thought, resp.usage.input_tokens,
                           resp.usage.output_tokens, resp.model, (time.time() - t0) * 1000)


# ---------------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatLLM:
    """OpenAI chat-completions API with tools. Works with OpenAI and with Ollama (http://localhost:11434/v1)."""

    name = "openai-compatible"

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "ollama")
        self.model = model or os.environ.get("ASAP_MODEL", "gpt-4.1-mini" if "openai.com" in self.base_url else "llama3.1")
        self.http = httpx.Client(timeout=60)

    @staticmethod
    def to_messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                tc = m["tool_call"]
                out.append({"role": "assistant", "content": m.get("text") or None, "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}]})
            else:
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def next(self, system: str, messages: list[dict], tools: list[dict], run: RunState, phase: str,
             timeout_s: float) -> LLMResponse:
        t0 = time.time()
        body = {"model": self.model, "messages": self.to_messages(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools],
                "tool_choice": "required"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=timeout_s)
            if r.status_code == 400 and "tool_choice" in r.text:
                body["tool_choice"] = "auto"  # some local servers don't support "required"
                r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=timeout_s)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"OpenAI-compatible endpoint error: {e}") from e
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            raise LLMUnavailable("model returned no tool call")
        fn = calls[0]["function"]
        args = fn["arguments"]
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError:
            args = {"_unparseable_arguments": args}
        usage = data.get("usage") or {}
        return LLMResponse(fn["name"], args, calls[0].get("id") or f"call_{int(t0 * 1000)}", msg.get("content") or "",
                           usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), data.get("model", self.model),
                           (time.time() - t0) * 1000)


def select_llm(choice: str = "auto") -> LLM:
    from .scripted import DeterministicReasoner

    if choice == "scripted":
        return DeterministicReasoner()
    if choice == "anthropic" or (choice == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        return AnthropicLLM()
    if choice in ("openai", "ollama") or (choice == "auto" and (os.environ.get("OPENAI_API_KEY") or
                                                               os.environ.get("OPENAI_BASE_URL"))):
        if choice == "ollama" and not os.environ.get("OPENAI_BASE_URL"):
            return OpenAICompatLLM(base_url="http://localhost:11434/v1")
        return OpenAICompatLLM()
    return DeterministicReasoner()
