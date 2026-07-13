from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Callable, Protocol

from .. import telemetry
from ..budget import Budget

# Tiers let the agent reason about cost/capability instead of model strings.
TIERS = {
    "smart": "claude-opus-4-8",
    "mid": "claude-sonnet-4-6",
    "cheap": "claude-haiku-4-5",
}

# Default output ceiling per tier. Smarter tiers get more room to reason and act;
# the cheap tier is for bulk work where short answers are the norm.
TIER_MAX_TOKENS = {
    "smart": 32000,
    "mid": 16000,
    "cheap": 8000,
}

# USD per token (input, output). Verify against current pricing as needed.
PRICING = {
    "claude-opus-4-8": (5.0 / 1e6, 25.0 / 1e6),
    "claude-sonnet-4-6": (3.0 / 1e6, 15.0 / 1e6),
    "claude-haiku-4-5": (1.0 / 1e6, 5.0 / 1e6),
}


def _supports_adaptive_thinking(model: str) -> bool:
    return "opus" in model or "sonnet" in model


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float

    @classmethod
    def from_response(cls, usage: object, model: str) -> "Usage":
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        in_rate, out_rate = PRICING.get(model, (0.0, 0.0))
        cost = (
            in_tok * in_rate
            + cache_create * 1.25 * in_rate
            + cache_read * 0.1 * in_rate
            + out_tok * out_rate
        )
        return cls(model, in_tok, out_tok, cost)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class Completion:
    text: str
    tool_calls: list[ToolCall]
    content: object  # raw provider content blocks, appended verbatim to history
    stop_reason: str | None = None


class LLM(Protocol):
    def complete(self, *, system, messages, tier=..., tools=..., max_tokens=..., on_token=...) -> Completion: ...


class AnthropicLLM:
    """Anthropic-backed LLM. Every call records usage to the shared Budget, so
    metering is centralized in one place regardless of who made the call (the
    orchestrator, the llm() capability, or a sub-agent)."""

    def __init__(self, budget: Budget | None = None, max_tokens: int = 8000):
        anthropic = import_module("anthropic")
        httpx = import_module("httpx")
        # A stalled stream must fail fast, not hang forever: `read` bounds the gap
        # between chunks, so a silent connection raises instead of blocking the
        # session indefinitely. It has to clear the worst legitimate quiet gap —
        # prefill over a large context plus adaptive thinking before the first
        # text chunk — or healthy long turns get killed mid-think; 240s covers
        # that headroom while still catching a truly dead socket. `max_retries`
        # lets the SDK transparently recover transient drops, which on flaky links
        # is the difference between a resumed turn and an aborted one.
        self._client = anthropic.Anthropic(
            timeout=httpx.Timeout(connect=10.0, read=240.0, write=20.0, pool=10.0),
            max_retries=4,
        )
        self._budget = budget
        self._max_tokens = max_tokens

    def _record(self, usage: object, model: str) -> Usage:
        u = Usage.from_response(usage, model)
        if self._budget is not None:
            self._budget.record(u.model, u.cost_usd)
        return u

    def complete(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        tier: str = "cheap",
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        on_token: "Callable[[str], None] | None" = None,
    ) -> Completion:
        model = TIERS.get(tier, tier)
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens or TIER_MAX_TOKENS.get(tier, self._max_tokens),
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if _supports_adaptive_thinking(model):
            kwargs["thinking"] = {"type": "adaptive"}

        # Stream and reassemble: the SDK refuses non-streaming requests whose
        # max_tokens could exceed its timeout, so streaming is what lets the
        # large per-tier ceilings above work.
        with telemetry.llm_span(model, tier, system=system, messages=messages):
            with self._client.messages.stream(**kwargs) as stream:
                if on_token is not None:
                    for chunk in stream.text_stream:
                        on_token(chunk)
                resp = stream.get_final_message()
            usage = self._record(resp.usage, model)

            text = "".join(b.text for b in resp.content if b.type == "text")
            tool_calls = [
                ToolCall(b.id, b.name, dict(b.input))
                for b in resp.content
                if b.type == "tool_use"
            ]
            telemetry.record_llm(
                model=model,
                tier=tier,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
                cache_create=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
                cost_usd=usage.cost_usd,
                output_text=text,
                tool_calls=[{"name": tc.name, "input": tc.input} for tc in tool_calls],
            )

        return Completion(text, tool_calls, resp.content, resp.stop_reason)

    def web_search(self, query: str, tier: str = "cheap", max_rounds: int = 6) -> str:
        """Search the web via Anthropic's server-side tool — no extra API key.

        Uses streaming so the HTTP connection stays active while the server
        executes the search (non-streaming hangs when the server goes silent
        during tool execution). The read timeout is widened well past the default
        completion budget: a server-side search can leave the stream quiet for far
        longer than prefill+thinking ever would, and killing it as a dead socket
        would surface as a spurious ReadTimeout."""
        httpx = import_module("httpx")
        client = self._client.with_options(
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=20.0, pool=10.0)
        )
        model = TIERS.get(tier, tier)
        messages: list[dict] = [{"role": "user", "content": query}]
        # allowed_callers=["direct"] makes the server run the search and inject
        # results directly, rather than the model driving it via programmatic tool
        # calling. Without it the API rejects the request on models that lack
        # programmatic tool calling (e.g. the cheap tier, claude-haiku-4-5), and
        # direct calling is what this one-shot query wants regardless of tier.
        tools = [{"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}]
        for _ in range(max_rounds):
            with client.messages.stream(
                model=model, max_tokens=4000, messages=messages, tools=tools
            ) as stream:
                resp = stream.get_final_message()
            self._record(resp.usage, model)
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": list(resp.content)})
                continue
            return "".join(b.text for b in resp.content if b.type == "text")
        return "(web_search: max rounds reached)"
