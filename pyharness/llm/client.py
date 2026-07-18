from __future__ import annotations

import random
import threading
import time
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


def _thinking_config(model: str) -> dict | None:
    """The `thinking` request param for a model, or None where unsupported.

    Opus 4.7+ defaults `display` to "omitted" (thinking blocks stream with
    empty text), so summaries must be requested explicitly — they are what the
    viewer streams during the otherwise-silent thinking spans. Sonnet 4.6
    already defaults to "summarized" and predates the `display` param, so it
    gets the bare adaptive config."""
    if "opus" in model:
        return {"type": "adaptive", "display": "summarized"}
    if "sonnet" in model:
        return {"type": "adaptive"}
    return None


def _cache_marked_messages(messages: list[dict], cache_anchor: int | None) -> list[dict]:
    """The request's message list with one `cache_control` breakpoint added,
    without mutating the caller's history — markers left on history dicts would
    accumulate across steps and blow the API's 4-breakpoint-per-request limit.

    The marker goes on the last content block of `messages[cache_anchor]` when
    given (the caller's newest byte-stable message — see Agent's elision
    frontier), else of the last message (full-history incremental caching:
    each step's entry extends the previous one). Together with the system
    breakpoint that is 2 of the 4 allowed markers. Prompts below the model's
    minimum cacheable prefix silently don't cache — the marker is harmless."""
    if not messages:
        return messages
    idx = cache_anchor if cache_anchor is not None and 0 <= cache_anchor < len(messages) else len(messages) - 1
    target = messages[idx]
    content = target.get("content") if isinstance(target, dict) else None
    if isinstance(content, str):
        if not content:
            return messages
        marked_content = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        marked_content = list(content)
        marked_content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    else:
        # SDK content-block objects (assistant turns) or empty content: skip
        # marking rather than guess at a mutation.
        return messages
    return [*messages[:idx], {**target, "content": marked_content}, *messages[idx + 1:]]


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
    def complete(
        self, *, system, messages, tier=..., tools=..., max_tokens=..., on_token=...,
        on_thinking=..., cache_anchor=...
    ) -> Completion: ...


# Streaming retry policy. The SDK's `max_retries` only covers failures before
# the response starts (connect errors, TTFB timeouts, 429/5xx); an exception
# raised while *iterating* the SSE body — the dominant observed failure, a
# read timeout on a stream that went silent — escapes it entirely. This wrapper
# retries the whole streamed completion. Prompt caching (below) makes the
# resend cheap: the failed attempt's prefill is already in the cache.
STREAM_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 8.0
_RETRYABLE_STATUS = {408, 409, 429, 529}

# Client-side stall detection. The httpx read timeout is a byte-gap bound that
# SSE ping events reset, so it cannot catch a wedged-but-pinging stream; and it
# fires on healthy quiet gaps (long adaptive thinking, cold prefill) exactly at
# the setting. The watchdog instead measures gaps between *stream events* and
# total attempt wall clock, and closes the response when either bound is
# breached — surfacing as a retryable `StreamStalled`. The deadline is a
# runaway backstop only: a legitimate 32k-token completion streams for 10+
# minutes, so it must sit well above that.
STALL_TIMEOUT_S = 180.0
ATTEMPT_DEADLINE_S = 1500.0


class StreamStalled(Exception):
    """A stream the watchdog killed: no events for STALL_TIMEOUT_S, or the
    whole attempt ran past ATTEMPT_DEADLINE_S. Transient by definition —
    the retry loop resends, and prompt caching makes the resend cheap."""


class _Watchdog:
    """Closes a streaming response when it stops making progress.

    `progress()` is called for every stream event; a checker thread closes the
    underlying httpx response when the event gap or the total attempt exceeds
    its bound, which makes the consuming iterator raise promptly. `fired` then
    tells the consumer the failure was watchdog-initiated so it can raise
    `StreamStalled` instead of the incidental close error."""

    def __init__(
        self,
        response,
        stall_timeout_s: float | None = None,
        deadline_s: float | None = None,
    ):
        self._response = response
        # Resolved at call time (not def time) so tests can shrink the module
        # constants; the tick scales down with them to keep detection prompt.
        self._stall_s = STALL_TIMEOUT_S if stall_timeout_s is None else stall_timeout_s
        self._deadline_s = ATTEMPT_DEADLINE_S if deadline_s is None else deadline_s
        self._tick = max(0.01, min(1.0, self._stall_s / 4, self._deadline_s / 4))
        self._start = time.monotonic()
        self._last = self._start
        self._done = threading.Event()
        self.fired: str | None = None  # "stalled" | "deadline"
        self._thread = threading.Thread(target=self._run, name="llm-watchdog", daemon=True)
        self._thread.start()

    def progress(self) -> None:
        self._last = time.monotonic()

    def stop(self) -> None:
        self._done.set()

    def _run(self) -> None:
        while not self._done.wait(self._tick):
            now = time.monotonic()
            if now - self._last >= self._stall_s:
                self.fired = "stalled"
            elif now - self._start >= self._deadline_s:
                self.fired = "deadline"
            else:
                continue
            try:
                if self._response is not None:
                    self._response.close()
            except Exception:  # noqa: BLE001 — the close is best-effort; the consumer errors either way
                pass
            return


class AnthropicLLM:
    """Anthropic-backed LLM. Every call records usage to the shared Budget, so
    metering is centralized in one place regardless of who made the call (the
    orchestrator, the llm() capability, or a sub-agent)."""

    def __init__(self, budget: Budget | None = None, max_tokens: int = 8000):
        anthropic = import_module("anthropic")
        httpx = import_module("httpx")
        # Stall detection is layered. The authoritative detector is the
        # `_Watchdog` in `_complete_once`: it counts stream *events* (which SSE
        # pings cannot reset) and the attempt's total wall clock. This `read`
        # timeout is only the transport backstop for full byte-silence — a dead
        # socket that sends nothing at all — and sits above the watchdog's
        # stall bound so the watchdog classifies first. SDK `max_retries` stays
        # low because complete() retries the whole streamed call
        # (STREAM_ATTEMPTS) — the two layers multiply, and the wrapper is the
        # one that also covers mid-stream failures the SDK never retries.
        self._client = anthropic.Anthropic(
            timeout=httpx.Timeout(connect=10.0, read=240.0, write=20.0, pool=10.0),
            max_retries=2,
        )
        self._anthropic = anthropic
        self._httpx = httpx
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
        clone._anthropic = self._anthropic
        clone._httpx = self._httpx
        clone._budget = budget
        clone._max_tokens = self._max_tokens
        return clone

    def _retryable(self, exc: Exception) -> bool:
        """Transient failures worth resending: transport-level errors (read
        timeouts, dropped connections, protocol violations), watchdog-killed
        stalls, SDK connection/timeout errors, and retryable HTTP statuses —
        529 overloaded and mid-stream `error` SSE events both surface as
        APIStatusError. 4xx request errors are deterministic and raise
        immediately."""
        if isinstance(exc, StreamStalled):
            return True
        if isinstance(exc, self._httpx.TransportError):
            return True
        if isinstance(exc, self._anthropic.APIConnectionError):  # includes APITimeoutError
            return True
        if isinstance(exc, self._anthropic.APIStatusError):
            return exc.status_code in _RETRYABLE_STATUS or exc.status_code >= 500
        return False

    def complete(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        tier: str = "cheap",
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        on_token: "Callable[[str], None] | None" = None,
        on_thinking: "Callable[[str], None] | None" = None,
        cache_anchor: int | None = None,
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
            "messages": _cache_marked_messages(messages, cache_anchor),
        }
        if system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            kwargs["tools"] = tools
        thinking = _thinking_config(model)
        if thinking is not None:
            kwargs["thinking"] = thinking

        for attempt in range(1, STREAM_ATTEMPTS + 1):
            try:
                return self._complete_once(kwargs, model, tier, system, messages, on_token, on_thinking)
            except Exception as exc:  # noqa: BLE001 — classified below
                if attempt == STREAM_ATTEMPTS or not self._retryable(exc):
                    raise
                if on_token is not None:
                    # Display-only marker (never part of the completion text):
                    # without it, the partial text the dead stream already
                    # emitted looks like the answer restarting on its own.
                    on_token(
                        f"\n[stream failed ({type(exc).__name__}); "
                        f"retry {attempt}/{STREAM_ATTEMPTS - 1}]\n"
                    )
                delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, 0.5))
        raise AssertionError("unreachable")  # pragma: no cover

    def _complete_once(
        self,
        kwargs: dict,
        model: str,
        tier: str,
        system: str | None,
        messages: list[dict],
        on_token: "Callable[[str], None] | None",
        on_thinking: "Callable[[str], None] | None",
    ) -> Completion:
        # Stream and reassemble: the SDK refuses non-streaming requests whose
        # max_tokens could exceed its timeout, so streaming is what lets the
        # large per-tier ceilings above work. The raw event stream is consumed
        # (never `text_stream`, which silently drops everything but text
        # deltas): thinking deltas reach `on_thinking` so adaptive-thinking
        # spans are visible instead of reading as a hang, and every event feeds
        # the watchdog as proof of progress. One telemetry span per attempt, so
        # a retried call shows up as a failed span plus a clean one.
        with telemetry.llm_span(model, tier, system=system, messages=messages):
            with self._client.messages.stream(**kwargs) as stream:
                watchdog = _Watchdog(getattr(stream, "response", None))
                try:
                    for event in stream:
                        watchdog.progress()
                        if event.type != "content_block_delta":
                            continue
                        delta = event.delta
                        if delta.type == "text_delta" and on_token is not None:
                            on_token(delta.text)
                        elif delta.type == "thinking_delta" and on_thinking is not None:
                            if delta.thinking:
                                on_thinking(delta.thinking)
                    resp = stream.get_final_message()
                except Exception as exc:
                    if watchdog.fired is not None:
                        # The consumer error is just the closed-response fallout;
                        # the real failure is the stall the watchdog detected.
                        raise StreamStalled(
                            f"stream {watchdog.fired}: no progress within "
                            f"{STALL_TIMEOUT_S:.0f}s or attempt over "
                            f"{ATTEMPT_DEADLINE_S:.0f}s"
                        ) from exc
                    raise
                finally:
                    watchdog.stop()
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
                cache_read=usage.cache_read_tokens,
                cache_create=usage.cache_creation_tokens,
                cost_usd=usage.cost_usd,
                output_text=text,
                tool_calls=[{"name": tc.name, "input": tc.input} for tc in tool_calls],
            )

        return Completion(text, tool_calls, resp.content, resp.stop_reason, usage)
