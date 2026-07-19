from __future__ import annotations

import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from importlib import import_module
from typing import Callable, Protocol, Sequence

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


def _cache_marked_system(system: "str | Sequence[str] | None") -> list[dict] | None:
    """The request's `system` as cache-marked text blocks, or None when empty.

    A single string is one block with a breakpoint at its end. A sequence of
    segments becomes one block each, every segment boundary a breakpoint — this
    is how the caller separates the byte-stable static prose from the per-turn
    dynamic preamble (date, workspace, spawn block). Without the split the whole
    system is one block whose single end-breakpoint sits *after* the changing
    preamble, so the ~1,600-word static prefix misses cache on every turn whose
    clock minute, workspace, or spawn budget differs; splitting lets the static
    block cache on its own. Empty segments drop out; if nothing survives, None.
    The caller keeps `system` to two segments so that with the message anchor
    this stays within the API's 4-breakpoint-per-request limit."""
    segments = [system] if isinstance(system, str) else list(system or [])
    blocks = [
        {"type": "text", "text": seg, "cache_control": {"type": "ephemeral"}}
        for seg in segments
        if seg
    ]
    return blocks or None


def _cache_marked_messages(messages: list[dict], cache_anchor: int | None) -> list[dict]:
    """The request's message list with one `cache_control` breakpoint added,
    without mutating the caller's history — markers left on history dicts would
    accumulate across steps and blow the API's 4-breakpoint-per-request limit.

    The marker goes on the last content block of `messages[cache_anchor]` when
    given (the caller's newest byte-stable message — see Agent's elision
    frontier), else of the last message (full-history incremental caching:
    each step's entry extends the previous one). Together with the (up to two)
    system breakpoints that is at most 3 of the 4 allowed markers. Prompts below
    the model's minimum cacheable prefix silently don't cache — marker harmless."""
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
    # True for a failed attempt metered from its streamed bytes: the API bills
    # it, but the exact output count never arrived, so it's estimated.
    estimated: bool = False

    @property
    def context_tokens(self) -> int:
        """The full prompt size the model consumed — cached and uncached."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @classmethod
    def from_response(
        cls,
        usage: object,
        model: str,
        output_tokens: int | None = None,
        estimated: bool = False,
    ) -> "Usage":
        """Usage from the API's usage object. For a killed attempt the exact
        output count never arrived, so the caller passes its own
        `output_tokens` estimate and marks the record `estimated`."""
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = output_tokens if output_tokens is not None else (getattr(usage, "output_tokens", 0) or 0)
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        in_rate, out_rate = PRICING.get(model, (0.0, 0.0))
        cost = (
            in_tok * in_rate
            + cache_create * 1.25 * in_rate
            + cache_read * 0.1 * in_rate
            + out_tok * out_rate
        )
        return cls(model, in_tok, out_tok, cost, cache_read, cache_create, estimated)


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
        on_thinking=..., on_attempt=..., cache_anchor=..., total_deadline_s=...
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
# A retry only launches if this much of the caller's total deadline is left —
# a sliver would just burn money to fail again.
_MIN_RETRY_HEADROOM_S = 30.0
_RETRYABLE_STATUS = {408, 409, 429, 529}

# Client-side stall detection: a reader thread iterates the stream and feeds a
# queue; the consuming thread — never blocked in a socket read — enforces three
# clocks, each with its own failure name:
#
#   silence  — byte-level liveness. The SDK drops SSE `ping` events at every
#              layer (`anthropic._streaming` skips them before yielding), so
#              the one place pings still register is the transport: they are
#              bytes, and bytes reset the httpx `read` timeout. Setting that
#              timeout to SILENCE_TIMEOUT_S makes the reader's blocking read
#              raise within 60s of true wire silence — the dead-connection
#              detector, down from the old 240s.
#   stall    — semantic progress, enforced by the consumer. If no stream
#              *events* arrive for STALL_TIMEOUT_S while bytes still flow
#              (else silence fires first at its lower bound), the server is
#              provably alive but generation is wedged.
#   deadline — runaway backstop, scaled to the request: an 8k-token worker
#              call may not legally run as long as a 32k smart call.
#
# All three surface as a retryable `StreamStalled` naming the clock that fired.
SILENCE_TIMEOUT_S = 60.0
STALL_TIMEOUT_S = 180.0
ATTEMPT_DEADLINE_BASE_S = 120.0
ATTEMPT_DEADLINE_PER_TOKEN_S = 0.03
ATTEMPT_DEADLINE_CAP_S = 1500.0
# Estimated chars per output token when metering a killed attempt (D2): the
# API reports exact output_tokens only at message_delta/end-of-stream, so a
# stream that died mid-generation is billed from what it streamed.
_CHARS_PER_TOKEN_EST = 4


def _attempt_deadline_s(max_tokens: int) -> float:
    return min(
        ATTEMPT_DEADLINE_CAP_S,
        ATTEMPT_DEADLINE_BASE_S + max_tokens * ATTEMPT_DEADLINE_PER_TOKEN_S,
    )


def _ipv4_first_client(httpx, timeout):
    """An httpx.Client pinned to IPv4 that retires the pin if IPv4 can't
    connect at all. `api.anthropic.com` is dual-stack and this SDK's httpx has
    no happy-eyeballs, so it commits to whichever family DNS lists first — and
    the 2026-07-18 D8 probe showed the IPv6 path silently dropping live streams
    (4/4 v6 dead mid-stream, 2/2 v4 clean, same network and minute). Pinning
    avoids that. The one cost of an unconditional pin is a v6-only network,
    where a v4 source address reaches nothing: on the first `ConnectError` the
    transport retires the pin and retries unpinned, so a v6-only host keeps
    working without the operator having to set PYHARNESS_LLM_IPV6."""
    class _IPv4FirstTransport(httpx.BaseTransport):
        def __init__(self):
            self._pinned = httpx.HTTPTransport(local_address="0.0.0.0")
            self._unpinned = httpx.HTTPTransport()
            self._v4_dead = False

        def handle_request(self, request):
            if not self._v4_dead:
                try:
                    return self._pinned.handle_request(request)
                except httpx.ConnectError:
                    self._v4_dead = True  # v6-only network: stop paying the probe
            return self._unpinned.handle_request(request)

        def close(self):
            self._pinned.close()
            self._unpinned.close()

    return httpx.Client(transport=_IPv4FirstTransport(), timeout=timeout)


class StreamStalled(Exception):
    """A stream the supervisor killed, naming which clock fired ("silence",
    "stall", "deadline", "truncated", or "total_deadline") plus what the
    attempt had streamed. Transient by definition — the retry loop resends,
    and prompt caching makes the resend cheap."""

    def __init__(
        self,
        message: str,
        *,
        clock: str = "stall",
        events: int = 0,
        output_tokens: int = 0,
        elapsed_s: float = 0.0,
    ):
        super().__init__(message)
        self.clock = clock
        self.events = events
        self.output_tokens = output_tokens
        self.elapsed_s = elapsed_s


class AnthropicLLM:
    """Anthropic-backed LLM. Every call records usage to the shared Budget, so
    metering is centralized in one place regardless of who made the call (the
    orchestrator, the llm() capability, or a sub-agent)."""

    def __init__(self, budget: Budget | None = None, max_tokens: int = 8000):
        anthropic = import_module("anthropic")
        httpx = import_module("httpx")
        # The `read` timeout IS the silence clock (see the constants above):
        # SSE pings are bytes, so a healthy-but-quiet stream keeps resetting
        # it, and true wire silence surfaces in the reader thread within
        # SILENCE_TIMEOUT_S. It also reaps reader threads abandoned after a
        # stall/deadline kill. SDK `max_retries` stays low because complete()
        # retries the whole streamed call (STREAM_ATTEMPTS) — the wrapper is
        # the layer that also covers mid-stream failures the SDK never retries.
        #
        # The transport binds to IPv4. `api.anthropic.com` is dual-stack and
        # this SDK's httpx has no happy-eyeballs, so it commits to whichever
        # family DNS lists first — and the 2026-07-18 D8 probe showed the IPv6
        # path silently dropping live streams (4/4 v6 attempts dead mid-stream,
        # 2/2 v4 clean, same network and minute). The pin retires itself on a
        # v6-only network (first connect failure falls back to unpinned), so
        # PYHARNESS_LLM_IPV6=true is only needed to force default family
        # selection, not to keep a v6-only host working.
        timeout = httpx.Timeout(connect=10.0, read=SILENCE_TIMEOUT_S, write=20.0, pool=10.0)
        client_kwargs: dict = {"timeout": timeout, "max_retries": 2}
        if os.environ.get("PYHARNESS_LLM_IPV6", "").lower() not in ("1", "true", "yes"):
            client_kwargs["http_client"] = _ipv4_first_client(httpx, timeout)
        self._client = anthropic.Anthropic(**client_kwargs)
        self._anthropic = anthropic
        self._httpx = httpx
        self._accumulate = import_module("anthropic.lib.streaming._messages").accumulate_event
        self._budget = budget
        self._max_tokens = max_tokens

    def _record(
        self,
        usage: object,
        model: str,
        *,
        output_tokens: int | None = None,
        estimated: bool = False,
    ) -> Usage:
        u = Usage.from_response(usage, model, output_tokens=output_tokens, estimated=estimated)
        if self._budget is not None:
            # Estimated (failed-attempt) spend counts too: the API billed it,
            # so excluding it would let a stall-heavy session overrun its cap.
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
        clone._accumulate = self._accumulate
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
        system: str | Sequence[str] | None = None,
        messages: list[dict],
        tier: str = "cheap",
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        on_token: "Callable[[str], None] | None" = None,
        on_thinking: "Callable[[str], None] | None" = None,
        on_attempt: "Callable[[dict], None] | None" = None,
        cache_anchor: int | None = None,
        total_deadline_s: float | None = None,
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
        # Validate max_tokens against the tier's output ceiling. Left unchecked,
        # max_tokens=0/negative silently fell through to the ceiling (and, after
        # the supervisor rewrite, pinned the attempt deadline at its minimum while
        # streaming at the full ceiling), and a value above the model's real
        # output limit produced a deterministic API 400. The tier ceiling is the
        # authoritative bound (well under every model's hard output cap), so a
        # positive request at or below it is honored and anything else is a clear
        # ValueError that surfaces straight to agent code via llm()/map_llm.
        ceiling = TIER_MAX_TOKENS.get(tier, self._max_tokens)
        if max_tokens is not None:
            if max_tokens <= 0:
                raise ValueError(
                    f"max_tokens must be a positive integer, got {max_tokens!r}"
                )
            if max_tokens > ceiling:
                raise ValueError(
                    f"max_tokens {max_tokens} exceeds the {tier!r} tier's output "
                    f"ceiling of {ceiling}; use a higher tier for more room"
                )
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens or ceiling,
            "messages": _cache_marked_messages(messages, cache_anchor),
        }
        system_blocks = _cache_marked_system(system)
        if system_blocks is not None:
            kwargs["system"] = system_blocks
        if tools:
            kwargs["tools"] = tools
        thinking = _thinking_config(model)
        if thinking is not None:
            kwargs["thinking"] = thinking

        # Telemetry and the trace want the flat prompt, not the cache-marked blocks.
        system_text = system if isinstance(system, str) else "".join(system or [])
        attempt_deadline = _attempt_deadline_s(kwargs["max_tokens"])
        started = time.monotonic()
        history: list[dict] = []  # one summary dict per finished attempt

        def fire(outcome: str, attempt: int, **extra) -> None:
            # Observability must never break the call itself.
            if on_attempt is None:
                return
            try:
                on_attempt({"attempt": attempt, "attempts": STREAM_ATTEMPTS, "outcome": outcome, **extra})
            except Exception:  # noqa: BLE001
                pass

        for attempt in range(1, STREAM_ATTEMPTS + 1):
            deadline = attempt_deadline
            if total_deadline_s is not None:
                deadline = min(deadline, total_deadline_s - (time.monotonic() - started))
            fire("start", attempt)
            stats = {"events": 0, "output_chars": 0, "output_tokens": 0}
            t0 = time.monotonic()
            try:
                completion = self._complete_once(
                    kwargs, model, tier, system_text, messages,
                    on_token, on_thinking, deadline, stats, fire, attempt,
                )
                fire(
                    "ok", attempt, elapsed_s=round(time.monotonic() - t0, 1),
                    events=stats["events"], output_tokens=stats["output_tokens"],
                )
                return completion
            except Exception as exc:  # noqa: BLE001 — classified below
                elapsed = time.monotonic() - t0
                summary = {
                    "attempt": attempt,
                    "error": type(exc).__name__,
                    "clock_fired": getattr(exc, "clock", None),
                    "elapsed_s": round(elapsed, 1),
                    "events": stats["events"],
                    "output_tokens": stats["output_tokens"],
                }
                history.append(summary)
                fire("failed", attempt, **{k: v for k, v in summary.items() if k != "attempt"})
                if attempt == STREAM_ATTEMPTS or not self._retryable(exc):
                    wrapped = self._with_history(exc, history)
                    if wrapped is exc:
                        raise
                    raise wrapped from exc
                delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                if total_deadline_s is not None:
                    # Stop when the next attempt cannot meaningfully fit: the
                    # caller bounded this whole call, and a sliver of leftover
                    # deadline would just burn money to fail again.
                    remaining = total_deadline_s - (time.monotonic() - started) - delay
                    if remaining < _MIN_RETRY_HEADROOM_S:
                        raise StreamStalled(
                            f"llm call gave up after {attempt} of {STREAM_ATTEMPTS} attempts: "
                            f"total deadline {total_deadline_s:.0f}s cannot fit another attempt"
                            f"\n{self._history_lines(history)}",
                            clock="total_deadline",
                            events=stats["events"],
                            output_tokens=stats["output_tokens"],
                            elapsed_s=elapsed,
                        ) from exc
                if on_token is not None:
                    # Display-only marker (never part of the completion text):
                    # without it, the partial text the dead stream already
                    # emitted looks like the answer restarting on its own.
                    on_token(
                        f"\n[stream failed ({type(exc).__name__}); "
                        f"retry {attempt}/{STREAM_ATTEMPTS - 1}]\n"
                    )
                time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _history_lines(history: list[dict]) -> str:
        return "\n".join(
            f"  attempt {h['attempt']}: {h['clock_fired'] or h['error']} "
            f"after {h['elapsed_s']}s ({h['events']} events, ~{h['output_tokens']} tokens out)"
            for h in history
        )

    def _with_history(self, exc: Exception, history: list[dict]) -> Exception:
        """The final failure, with the per-attempt summary appended when the
        exception is ours to shape — so 'what did the retries actually do'
        never needs a debugger again. Foreign exception types re-raise as-is."""
        if isinstance(exc, StreamStalled) and len(history) > 1:
            return StreamStalled(
                f"{exc}\n{self._history_lines(history)}",
                clock=exc.clock,
                events=exc.events,
                output_tokens=exc.output_tokens,
                elapsed_s=exc.elapsed_s,
            )
        return exc

    def _complete_once(
        self,
        kwargs: dict,
        model: str,
        tier: str,
        system: str | None,
        messages: list[dict],
        on_token: "Callable[[str], None] | None",
        on_thinking: "Callable[[str], None] | None",
        deadline_s: float,
        stats: dict,
        fire: "Callable[..., None]",
        attempt: int,
    ) -> Completion:
        # Stream and reassemble: the SDK refuses non-streaming requests whose
        # max_tokens could exceed its timeout, so streaming is what lets the
        # large per-tier ceilings above work. A daemon reader thread iterates
        # the raw event stream and feeds a queue; this (consuming) thread is
        # therefore never blocked in a socket read and enforces the stall and
        # deadline clocks precisely — the silence clock lives in the transport
        # read timeout and surfaces here through the reader (see the constants
        # above). The raw stream is consumed (never `text_stream`): thinking
        # deltas reach `on_thinking` so adaptive-thinking spans are visible
        # instead of reading as a hang. One telemetry span per attempt, so a
        # retried call shows up as a failed span plus a clean one.
        with telemetry.llm_span(model, tier, system=system, messages=messages):
            raw = self._client.messages.create(**kwargs, stream=True)
            events: queue.Queue = queue.Queue()  # unbounded: an abandoned reader must never block on put
            abandoned = threading.Event()

            def _read() -> None:
                try:
                    for ev in raw:
                        events.put(("event", ev))
                        if abandoned.is_set():
                            return
                    events.put(("end", None))
                except BaseException as exc:  # noqa: BLE001 — handed to the consumer to classify
                    events.put(("error", exc))

            threading.Thread(target=_read, name="llm-stream-reader", daemon=True).start()

            snapshot = None
            start = last_event = last_beat = time.monotonic()
            try:
                while True:
                    now = time.monotonic()
                    if now - start >= deadline_s:
                        raise StreamStalled(
                            f"stream deadline: attempt still running after {deadline_s:.0f}s",
                            clock="deadline", events=stats["events"],
                            output_tokens=stats["output_tokens"], elapsed_s=now - start,
                        )
                    if now - last_event >= STALL_TIMEOUT_S:
                        # Bytes are still flowing (wire silence would have hit the
                        # transport read timeout first, at its lower bound), so the
                        # server is alive but generation has wedged.
                        raise StreamStalled(
                            f"stream stall: connection alive but no events for {STALL_TIMEOUT_S:.0f}s",
                            clock="stall", events=stats["events"],
                            output_tokens=stats["output_tokens"], elapsed_s=now - start,
                        )
                    if now - last_beat >= 30.0:
                        last_beat = now
                        fire(
                            "streaming", attempt, elapsed_s=round(now - start, 1),
                            events=stats["events"], output_tokens=stats["output_tokens"],
                        )
                    try:
                        kind, payload = events.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if kind == "error":
                        if isinstance(payload, self._httpx.ReadTimeout):
                            now = time.monotonic()
                            raise StreamStalled(
                                f"stream silence: no bytes on the wire for {SILENCE_TIMEOUT_S:.0f}s "
                                "(SSE pings included) — connection is dead",
                                clock="silence", events=stats["events"],
                                output_tokens=stats["output_tokens"], elapsed_s=now - start,
                            ) from payload
                        raise payload
                    if kind == "end":
                        if snapshot is None or snapshot.stop_reason is None:
                            # The server closed the stream without finishing the
                            # message — a half answer must not pass for a whole one.
                            raise StreamStalled(
                                "stream truncated: ended before message completion",
                                clock="truncated", events=stats["events"],
                                output_tokens=stats["output_tokens"],
                                elapsed_s=time.monotonic() - start,
                            )
                        break
                    ev = payload
                    last_event = time.monotonic()
                    snapshot = self._accumulate(event=ev, current_snapshot=snapshot)
                    stats["events"] += 1
                    if ev.type == "content_block_delta":
                        delta = ev.delta
                        if delta.type == "text_delta":
                            stats["output_chars"] += len(delta.text)
                            if on_token is not None:
                                on_token(delta.text)
                        elif delta.type == "thinking_delta" and delta.thinking:
                            # Thinking is billed output too — count it toward the
                            # failed-attempt estimate.
                            stats["output_chars"] += len(delta.thinking)
                            if on_thinking is not None:
                                on_thinking(delta.thinking)
                    if ev.type == "message_delta":
                        stats["output_tokens"] = getattr(snapshot.usage, "output_tokens", 0) or 0
                    else:
                        stats["output_tokens"] = max(
                            stats["output_tokens"], stats["output_chars"] // _CHARS_PER_TOKEN_EST
                        )
            except BaseException:
                abandoned.set()
                try:
                    raw.close()
                except Exception:  # noqa: BLE001 — best-effort; the reader dies on its own read timeout
                    pass
                # A killed attempt still spent real money: meter what the API
                # already processed (prompt from message_start, output estimated
                # from streamed bytes) so Budget and by_model stay truthful.
                if snapshot is not None:
                    self._record(
                        snapshot.usage, model,
                        output_tokens=stats["output_tokens"], estimated=True,
                    )
                raise

            resp = snapshot
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
