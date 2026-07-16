from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Callable, Protocol

from ..obs import telemetry
from ..budget import Budget

# Provider credentials the *parent* holds so it can call the LLM or a search
# provider on the agent's behalf. The child never makes these calls itself — they
# route through the broker back to the parent — so these keys are stripped from the
# child's environment and from any bash subprocess. Otherwise agent code could read
# a live key straight from `os.environ` or `printenv`, sidestepping the vault's
# "cleartext never reaches the agent" contract.
PROVIDER_SECRET_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "EXA_API_KEY",  # web.search_results (Exa) — held parent-side like the LLM keys
)

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
    input_tokens: int  # uncached prompt tokens only, per the API's accounting
    output_tokens: int
    cost_usd: float
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def context_tokens(self) -> int:
        """The full prompt size the model consumed — cached and uncached."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

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
        return cls(model, in_tok, out_tok, cost, cache_read, cache_create)


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
    usage: Usage | None = None  # token/cost accounting for this call, when known


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

    def with_budget(self, budget: Budget) -> "AnthropicLLM":
        """A sibling client on the same underlying connection, metering into a
        different Budget — how a spawned child session gets its own slice
        accounted separately from its parent's."""
        clone = object.__new__(AnthropicLLM)
        clone._client = self._client
        clone._budget = budget
        clone._max_tokens = self._max_tokens
        return clone

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
        # A tier names a cost/capability band, never a raw model id. Resolving an
        # unknown tier to the literal string (the old `TIERS.get(tier, tier)`) let
        # agent-controlled code run an arbitrary, unpriced model: it would execute
        # against the operator's account while `PRICING.get(model, (0.0, 0.0))`
        # billed it at $0, so `Budget.check()` never tripped — an accounting bypass
        # and unbounded real spend. Fail closed on an unknown tier instead.
        model = TIERS.get(tier)
        if model is None:
            raise ValueError(
                f"unknown tier {tier!r}; choose one of {sorted(TIERS)} "
                "(a tier names a cost/capability band, not a model id)"
            )
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

        return Completion(text, tool_calls, resp.content, resp.stop_reason, usage)
