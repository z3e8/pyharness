"""AnthropicLLM reliability: the stream supervisor, retries, partial-usage
metering, cache markers, error classes.

All offline — the SDK client object is replaced with scripted fakes feeding
real SDK raw-event models (accumulate_event needs them); only the exception
classes, event accumulation, and request-shaping logic are real.
"""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import anthropic
from anthropic.types import (
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolUseBlock,
    InputJSONDelta,
    Usage as APIUsage,
)
from anthropic.types.raw_message_delta_event import Delta

from pyharness.budget import Budget
from pyharness.llm.client import (
    STREAM_ATTEMPTS,
    AnthropicLLM,
    StreamStalled,
    Usage,
    _attempt_deadline_s,
    _cache_marked_messages,
    _cache_marked_system,
)


# ---- raw-event builders ------------------------------------------------------


def _msg_start(input_tokens=10, cache_read=0, cache_create=0):
    return RawMessageStartEvent.construct(
        type="message_start",
        message=Message.construct(
            id="msg_test",
            content=[],
            model="claude-haiku-4-5",
            role="assistant",
            stop_reason=None,
            stop_sequence=None,
            type="message",
            usage=APIUsage.construct(
                input_tokens=input_tokens,
                output_tokens=1,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
            ),
        ),
    )


def _block_start(index=0, block=None):
    return RawContentBlockStartEvent.construct(
        type="content_block_start",
        index=index,
        content_block=block or TextBlock.construct(type="text", text="", citations=None),
    )


def _text_event(text, index=0):
    return RawContentBlockDeltaEvent.construct(
        type="content_block_delta",
        index=index,
        delta=TextDelta.construct(type="text_delta", text=text),
    )


def _think_event(text, index=0):
    return RawContentBlockDeltaEvent.construct(
        type="content_block_delta",
        index=index,
        delta=ThinkingDelta.construct(type="thinking_delta", thinking=text),
    )


def _json_event(partial_json, index=0):
    return RawContentBlockDeltaEvent.construct(
        type="content_block_delta",
        index=index,
        delta=InputJSONDelta.construct(type="input_json_delta", partial_json=partial_json),
    )


def _block_stop(index=0):
    return RawContentBlockStopEvent.construct(type="content_block_stop", index=index)


def _msg_delta(stop_reason="end_turn", output_tokens=5):
    return RawMessageDeltaEvent.construct(
        type="message_delta",
        delta=Delta.construct(stop_reason=stop_reason, stop_sequence=None, stop_details=None),
        usage=MessageDeltaUsage.construct(
            output_tokens=output_tokens,
            input_tokens=None,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
            server_tool_use=None,
        ),
    )


def _msg_stop():
    return RawMessageStopEvent.construct(type="message_stop")


def _ok_events(tokens=("ok",), stop_reason="end_turn", output_tokens=5, **start_kw):
    return [
        _msg_start(**start_kw),
        _block_start(),
        *[_text_event(t) for t in tokens],
        _block_stop(),
        _msg_delta(stop_reason, output_tokens),
        _msg_stop(),
    ]


# ---- fakes -------------------------------------------------------------------


class FakeRawStream:
    """One scripted raw-event stream attempt: yields `events`, then either
    raises `exc` (a mid-stream failure) or ends normally."""

    def __init__(self, events=(), exc=None):
        self.events = list(events)
        self.exc = exc
        self.closed = False

    def __iter__(self):
        yield from self.events
        if self.exc is not None:
            raise self.exc

    def close(self):
        self.closed = True


class HangingRawStream:
    """Streams a healthy prefix then blocks until close() — the shape of a
    wedged-but-alive connection (bytes/pings still flow at the transport, so
    the read timeout never fires; only the consumer's stall clock can act)."""

    def __init__(self):
        self._closed = threading.Event()

    def __iter__(self):
        yield _msg_start()
        yield _block_start()
        yield _text_event("par")
        if not self._closed.wait(5.0):
            raise AssertionError("supervisor never closed the stream")
        raise httpx.ReadError("connection closed")

    def close(self):
        self._closed.set()


class BabblingRawStream:
    """Emits events forever without finishing — runaway generation that keeps
    resetting the stall clock; only the attempt deadline catches it."""

    def __init__(self):
        self._closed = threading.Event()

    def __iter__(self):
        yield _msg_start()
        yield _block_start()
        while not self._closed.is_set():
            yield _text_event(".")
            time.sleep(0.005)
        raise httpx.ReadError("connection closed")

    def close(self):
        self._closed.set()


class FakeMessages:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    def create(self, *, stream, **kwargs):
        assert stream is True
        self.calls.append(kwargs)
        return self.streams.pop(0)


def _llm(monkeypatch, streams, budget=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("pyharness.llm.client.time.sleep", lambda s: None)
    llm = AnthropicLLM(budget=budget)
    llm._client = SimpleNamespace(messages=FakeMessages(streams))
    return llm


def _status_error(status):
    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("scripted", response=response, body=None)


# ---- retry policy ------------------------------------------------------------


def test_mid_stream_timeout_is_retried_until_success(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start()], exc=httpx.ReadTimeout("silent stream")),
        FakeRawStream(events=[_msg_start(), _block_start()], exc=httpx.ReadTimeout("silent stream")),
        FakeRawStream(events=_ok_events(tokens=("answer",))),
    ])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}], tier="cheap")
    assert completion.text == "answer"
    assert len(llm._client.messages.calls) == 3


def test_overloaded_status_is_retried(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeRawStream(exc=_status_error(529)),
        FakeRawStream(events=_ok_events()),
    ])
    assert llm.complete(messages=[{"role": "user", "content": "hi"}]).text == "ok"
    assert len(llm._client.messages.calls) == 2


def test_bad_request_is_not_retried(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeRawStream(exc=_status_error(400)),
        FakeRawStream(events=_ok_events()),  # must never be reached
    ])
    with pytest.raises(anthropic.APIStatusError):
        llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert len(llm._client.messages.calls) == 1


def test_exhausted_retries_raise_stalled_with_attempt_history(monkeypatch):
    # Wire silence surfaces as the reader's ReadTimeout and is classified as
    # the silence clock; after the last attempt the error carries a
    # per-attempt summary so triage never needs a debugger.
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start(), _text_event("ab")],
                      exc=httpx.ReadTimeout("dead"))
        for _ in range(STREAM_ATTEMPTS)
    ])
    with pytest.raises(StreamStalled) as err:
        llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert err.value.clock == "silence"
    assert "attempt 1: silence" in str(err.value)
    assert f"attempt {STREAM_ATTEMPTS}: silence" in str(err.value)
    assert len(llm._client.messages.calls) == STREAM_ATTEMPTS


def test_retry_emits_display_marker_not_answer_text(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start(), _text_event("par"), _text_event("tial")],
                      exc=httpx.ReadTimeout("dead")),
        FakeRawStream(events=_ok_events(tokens=("clean ", "answer"))),
    ])
    seen = []
    completion = llm.complete(
        messages=[{"role": "user", "content": "hi"}], on_token=seen.append
    )
    assert completion.text == "clean answer"
    assert any("[stream failed" in chunk for chunk in seen)
    assert seen.index("tial") < [i for i, c in enumerate(seen) if "[stream failed" in c][0]


def test_unknown_tier_fails_closed(monkeypatch):
    llm = _llm(monkeypatch, [])
    with pytest.raises(ValueError, match="unknown tier"):
        llm.complete(messages=[{"role": "user", "content": "hi"}], tier="claude-opus-4-8")


# ---- request shaping: cache markers, thinking config -------------------------


def test_request_carries_cache_markers(monkeypatch):
    llm = _llm(monkeypatch, [FakeRawStream(events=_ok_events())])
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": [{"type": "text", "text": "step"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]},
    ]
    llm.complete(system="sys prompt", messages=messages, tier="cheap")

    kwargs = llm._client.messages.calls[0]
    assert kwargs["system"] == [
        {"type": "text", "text": "sys prompt", "cache_control": {"type": "ephemeral"}}
    ]
    sent_last = kwargs["messages"][-1]["content"][-1]
    assert sent_last["cache_control"] == {"type": "ephemeral"}
    # The caller's history must stay pristine — stale markers would accumulate
    # past the API's 4-breakpoint limit.
    assert "cache_control" not in messages[-1]["content"][-1]
    assert messages[0]["content"] == "task"


def test_cache_marked_system_segments_get_a_breakpoint_each():
    blocks = _cache_marked_system(["static prose", "\n\ndynamic preamble"])
    assert blocks == [
        {"type": "text", "text": "static prose", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "\n\ndynamic preamble", "cache_control": {"type": "ephemeral"}},
    ]


def test_cache_marked_system_string_is_one_block():
    assert _cache_marked_system("just a string") == [
        {"type": "text", "text": "just a string", "cache_control": {"type": "ephemeral"}}
    ]


def test_cache_marked_system_drops_empties():
    assert _cache_marked_system(["kept", ""]) == [
        {"type": "text", "text": "kept", "cache_control": {"type": "ephemeral"}}
    ]
    assert _cache_marked_system(None) is None
    assert _cache_marked_system("") is None
    assert _cache_marked_system(["", ""]) is None


def test_request_carries_split_system_breakpoints(monkeypatch):
    llm = _llm(monkeypatch, [FakeRawStream(events=_ok_events())])
    llm.complete(
        system=["static", "\n\ndynamic"],
        messages=[{"role": "user", "content": "task"}],
        tier="cheap",
    )
    sent = llm._client.messages.calls[0]["system"]
    assert [b["text"] for b in sent] == ["static", "\n\ndynamic"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in sent)


def test_cache_anchor_marks_the_anchor_message(monkeypatch):
    llm = _llm(monkeypatch, [FakeRawStream(events=_ok_events())])
    messages = [
        {"role": "user", "content": "task"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[output elided: 900 chars]"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "fresh output"}]},
    ]
    llm.complete(messages=messages, tier="cheap", cache_anchor=1)

    sent = llm._client.messages.calls[0]["messages"]
    assert sent[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent[2]["content"][-1]
    assert "cache_control" not in messages[1]["content"][-1]


def test_cache_marker_wraps_string_content():
    marked = _cache_marked_messages([{"role": "user", "content": "hello"}], None)
    assert marked[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]


def test_cache_marker_skips_unmarkable_content():
    sdk_blocks = [SimpleNamespace(type="text", text="x")]
    messages = [{"role": "assistant", "content": sdk_blocks}]
    assert _cache_marked_messages(messages, None) is messages
    assert _cache_marked_messages([], None) == []


def test_cache_marker_out_of_range_anchor_falls_back_to_last():
    messages = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ]
    marked = _cache_marked_messages(messages, 99)
    assert marked[0]["content"] == "a"
    assert marked[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_thinking_deltas_reach_on_thinking_not_on_token(monkeypatch):
    from anthropic.types import ThinkingBlock

    events = [
        _msg_start(),
        _block_start(block=ThinkingBlock.construct(type="thinking", thinking="", signature="")),
        _think_event("mull "),
        _think_event(""),
        _think_event("it over"),
        _block_stop(),
        _block_start(index=1),
        _text_event("out", index=1),
        _block_stop(index=1),
        _msg_delta(),
        _msg_stop(),
    ]
    llm = _llm(monkeypatch, [FakeRawStream(events=events)])
    thoughts, tokens = [], []
    completion = llm.complete(
        messages=[{"role": "user", "content": "hi"}],
        on_token=tokens.append,
        on_thinking=thoughts.append,
    )
    assert completion.text == "out"
    assert thoughts == ["mull ", "it over"]  # empty deltas are dropped
    assert tokens == ["out"]


def test_thinking_config_per_tier(monkeypatch):
    llm = _llm(monkeypatch, [FakeRawStream(events=_ok_events()) for _ in range(3)])
    for tier in ("smart", "mid", "cheap"):
        llm.complete(messages=[{"role": "user", "content": "hi"}], tier=tier)
    calls = llm._client.messages.calls
    assert calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert calls[1]["thinking"] == {"type": "adaptive"}
    assert "thinking" not in calls[2]


def test_tool_use_input_is_reassembled(monkeypatch):
    events = [
        _msg_start(),
        _block_start(block=ToolUseBlock.construct(type="tool_use", id="t1", name="run_python", input={})),
        _json_event('{"code": '),
        _json_event('"print(1)"}'),
        _block_stop(),
        _msg_delta(stop_reason="tool_use"),
        _msg_stop(),
    ]
    llm = _llm(monkeypatch, [FakeRawStream(events=events)])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.stop_reason == "tool_use"
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].name == "run_python"
    assert completion.tool_calls[0].input == {"code": "print(1)"}


# ---- supervisor: the three clocks --------------------------------------------


def test_silent_death_is_classified_as_silence(monkeypatch):
    # Wire silence surfaces as the reader thread's ReadTimeout (pings are
    # bytes, so a healthy-but-quiet stream cannot trip it) and is renamed to
    # the silence clock.
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start()], exc=httpx.ReadTimeout("dead")),
        FakeRawStream(events=_ok_events(tokens=("saved",))),
    ])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "saved"
    assert len(llm._client.messages.calls) == 2


def test_wedged_stream_is_killed_by_the_stall_clock(monkeypatch):
    monkeypatch.setattr("pyharness.llm.client.STALL_TIMEOUT_S", 0.05)
    llm = _llm(monkeypatch, [HangingRawStream(), FakeRawStream(events=_ok_events(tokens=("saved",)))])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "saved"
    assert len(llm._client.messages.calls) == 2


def test_runaway_stream_hits_the_attempt_deadline(monkeypatch):
    monkeypatch.setattr("pyharness.llm.client.ATTEMPT_DEADLINE_BASE_S", 0.1)
    monkeypatch.setattr("pyharness.llm.client.ATTEMPT_DEADLINE_PER_TOKEN_S", 0.0)
    llm = _llm(monkeypatch, [BabblingRawStream(), FakeRawStream(events=_ok_events(tokens=("saved",)))])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "saved"
    assert len(llm._client.messages.calls) == 2


def test_slow_but_progressing_stream_is_not_killed(monkeypatch):
    # The false-positive regression: events that keep arriving inside the
    # stall bound must never be treated as a wedge, however slow the pace.
    monkeypatch.setattr("pyharness.llm.client.STALL_TIMEOUT_S", 0.5)

    class TricklingStream(FakeRawStream):
        def __iter__(self):
            for ev in self.events:
                time.sleep(0.02)
                yield ev

    llm = _llm(monkeypatch, [TricklingStream(events=_ok_events(tokens=tuple("slowly")))])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "slowly"


def test_truncated_stream_is_retried_not_returned(monkeypatch):
    # A stream that ends cleanly but before message completion is a half
    # answer — it must retry, never pass for the real thing.
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start(), _text_event("half")]),
        FakeRawStream(events=_ok_events(tokens=("whole",))),
    ])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "whole"
    assert len(llm._client.messages.calls) == 2


def test_attempt_deadline_scales_with_max_tokens():
    assert _attempt_deadline_s(8000) == pytest.approx(360.0)
    assert _attempt_deadline_s(32000) == pytest.approx(1080.0)
    assert _attempt_deadline_s(10**6) == 1500.0  # cap


def test_total_deadline_stops_further_attempts(monkeypatch):
    # With ~10s of total budget left, the ~30s retry headroom rule stops the
    # call after the first failure instead of burning two more attempts.
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start()], exc=httpx.ReadTimeout("dead")),
        FakeRawStream(events=_ok_events()),  # must never be reached
    ])
    with pytest.raises(StreamStalled) as err:
        llm.complete(messages=[{"role": "user", "content": "hi"}], total_deadline_s=10.0)
    assert err.value.clock == "total_deadline"
    assert "attempt 1" in str(err.value)
    assert len(llm._client.messages.calls) == 1


def test_stream_stalled_is_retryable(monkeypatch):
    llm = _llm(monkeypatch, [])
    assert llm._retryable(StreamStalled("stalled"))


# ---- partial-usage metering (failed attempts cost real money) ----------------


def test_failed_attempts_are_metered_into_the_budget(monkeypatch):
    budget = Budget()
    streams = [
        FakeRawStream(
            events=[_msg_start(input_tokens=100, cache_read=50), _block_start(),
                    _text_event("x" * 40)],
            exc=httpx.ReadTimeout("dead"),
        )
        for _ in range(STREAM_ATTEMPTS)
    ]
    llm = _llm(monkeypatch, streams, budget=budget)
    with pytest.raises(StreamStalled):
        llm.complete(messages=[{"role": "user", "content": "hi"}], tier="cheap")
    # Each attempt burned 100 uncached + 50 cached-read input and ~10 output
    # tokens (40 chars / 4) at haiku pricing.
    per_attempt = 100 * 1e-6 + 50 * 0.1 * 1e-6 + 10 * 5e-6
    assert budget.spent_usd == pytest.approx(STREAM_ATTEMPTS * per_attempt)
    assert budget.by_model == {"claude-haiku-4-5": pytest.approx(STREAM_ATTEMPTS * per_attempt)}


def test_successful_usage_is_exact_not_estimated(monkeypatch):
    budget = Budget()
    llm = _llm(monkeypatch, [FakeRawStream(events=_ok_events(output_tokens=7))], budget=budget)
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.usage.estimated is False
    assert completion.usage.output_tokens == 7
    assert budget.calls == 1


def test_partial_usage_is_marked_estimated():
    u = Usage.from_response(
        SimpleNamespace(input_tokens=5, output_tokens=1,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        "claude-haiku-4-5",
        output_tokens=42,
        estimated=True,
    )
    assert u.estimated is True
    assert u.output_tokens == 42


# ---- attempt visibility (on_attempt) -----------------------------------------


def test_on_attempt_fires_start_failed_start_ok(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeRawStream(events=[_msg_start(), _block_start(), _text_event("abcd")],
                      exc=httpx.ReadTimeout("dead")),
        FakeRawStream(events=_ok_events(tokens=("fine",), output_tokens=3)),
    ])
    fired = []
    completion = llm.complete(
        messages=[{"role": "user", "content": "hi"}], on_attempt=fired.append
    )
    assert completion.text == "fine"
    assert [(f["attempt"], f["outcome"]) for f in fired] == [
        (1, "start"), (1, "failed"), (2, "start"), (2, "ok"),
    ]
    failed = fired[1]
    assert failed["clock_fired"] == "silence"
    assert failed["events"] == 3
    assert failed["output_tokens"] == 1  # 4 streamed chars / 4
    ok = fired[3]
    assert ok["output_tokens"] == 3
    assert ok["events"] == len(_ok_events(tokens=("fine",)))


def test_on_attempt_errors_never_break_the_call(monkeypatch):
    llm = _llm(monkeypatch, [FakeRawStream(events=_ok_events())])

    def explode(info):
        raise RuntimeError("observer bug")

    assert llm.complete(messages=[{"role": "user", "content": "hi"}], on_attempt=explode).text == "ok"
