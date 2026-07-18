"""AnthropicLLM reliability: stream retries, cache markers, error classes.

All offline — the SDK client object is replaced with scripted fakes; only the
exception classes and request-shaping logic are real.
"""

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import anthropic

from pyharness.llm.client import (
    STREAM_ATTEMPTS,
    AnthropicLLM,
    StreamStalled,
    _cache_marked_messages,
)


def _response(text="ok"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _text_event(text):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


def _think_event(text):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking=text)
    )


class FakeStream:
    """One scripted stream attempt: yields text events for `tokens` (plus any
    explicit `events`), then either raises `exc` (a mid-stream failure) or
    returns `resp` from get_final_message()."""

    def __init__(self, resp=None, exc=None, tokens=(), events=None):
        self.resp = resp
        self.exc = exc
        self.events = list(events) if events is not None else [_text_event(t) for t in tokens]
        self.response = SimpleNamespace(close=lambda: None)  # watchdog target

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        yield from self.events
        if self.exc is not None:
            raise self.exc

    def get_final_message(self):
        return self.resp


class FakeMessages:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self.streams.pop(0)


def _llm(monkeypatch, streams):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("pyharness.llm.client.time.sleep", lambda s: None)
    llm = AnthropicLLM()
    llm._client = SimpleNamespace(messages=FakeMessages(streams))
    return llm


def _status_error(status):
    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("scripted", response=response, body=None)


def test_mid_stream_timeout_is_retried_until_success(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeStream(exc=httpx.ReadTimeout("silent stream")),
        FakeStream(exc=httpx.ReadTimeout("silent stream")),
        FakeStream(resp=_response("answer")),
    ])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}], tier="cheap")
    assert completion.text == "answer"
    assert len(llm._client.messages.calls) == 3


def test_overloaded_status_is_retried(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeStream(exc=_status_error(529)),
        FakeStream(resp=_response()),
    ])
    assert llm.complete(messages=[{"role": "user", "content": "hi"}]).text == "ok"
    assert len(llm._client.messages.calls) == 2


def test_bad_request_is_not_retried(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeStream(exc=_status_error(400)),
        FakeStream(resp=_response()),  # must never be reached
    ])
    with pytest.raises(anthropic.APIStatusError):
        llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert len(llm._client.messages.calls) == 1


def test_exhausted_retries_raise_the_last_error(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeStream(exc=httpx.ReadTimeout("dead")) for _ in range(STREAM_ATTEMPTS)
    ])
    with pytest.raises(httpx.ReadTimeout):
        llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert len(llm._client.messages.calls) == STREAM_ATTEMPTS


def test_retry_emits_display_marker_not_answer_text(monkeypatch):
    # Partial tokens stream, the stream dies, the retry succeeds: the consumer
    # sees a retry marker between the orphaned partial and the fresh stream,
    # and the completion text comes solely from the final message.
    llm = _llm(monkeypatch, [
        FakeStream(exc=httpx.ReadTimeout("dead"), tokens=("par", "tial")),
        FakeStream(resp=_response("clean answer"), tokens=("clean ", "answer")),
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


def test_request_carries_cache_markers(monkeypatch):
    llm = _llm(monkeypatch, [FakeStream(resp=_response())])
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


def test_cache_anchor_marks_the_anchor_message(monkeypatch):
    llm = _llm(monkeypatch, [FakeStream(resp=_response())])
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
    # SDK content-block objects (assistant turns) aren't dicts — leave alone.
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


# ---- raw event stream: thinking surfacing ------------------------------------


def test_thinking_deltas_reach_on_thinking_not_on_token(monkeypatch):
    llm = _llm(monkeypatch, [
        FakeStream(
            resp=_response("out"),
            events=[_think_event("mull "), _think_event(""), _think_event("it over"),
                    _text_event("out")],
        ),
    ])
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
    llm = _llm(monkeypatch, [FakeStream(resp=_response()) for _ in range(3)])
    for tier in ("smart", "mid", "cheap"):
        llm.complete(messages=[{"role": "user", "content": "hi"}], tier=tier)
    calls = llm._client.messages.calls
    # opus defaults display to omitted — summaries must be requested; sonnet 4.6
    # already defaults to summarized and predates the display param; haiku has
    # no adaptive thinking at all.
    assert calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert calls[1]["thinking"] == {"type": "adaptive"}
    assert "thinking" not in calls[2]


# ---- watchdog: stalls and runaway attempts -----------------------------------


class HangingStream:
    """Blocks mid-stream until the watchdog closes the response, then raises —
    the shape of a wedged-but-pinging connection (bytes flow, no events, so the
    httpx read timeout never fires)."""

    def __init__(self):
        self._closed = threading.Event()
        self.response = SimpleNamespace(close=self._closed.set)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        yield _text_event("par")
        if not self._closed.wait(5.0):
            raise AssertionError("watchdog never closed the response")
        raise httpx.ReadError("connection closed")

    def get_final_message(self):  # pragma: no cover — iteration always raises
        raise AssertionError("unreachable")


class BabblingStream:
    """Emits events forever without finishing — runaway generation that keeps
    resetting the stall detector; only the attempt deadline catches it."""

    def __init__(self):
        self._closed = threading.Event()
        self.response = SimpleNamespace(close=self._closed.set)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        while not self._closed.is_set():
            yield _text_event(".")
            time.sleep(0.005)
        raise httpx.ReadError("connection closed")

    def get_final_message(self):  # pragma: no cover — iteration always raises
        raise AssertionError("unreachable")


def test_stalled_stream_is_killed_and_retried(monkeypatch):
    monkeypatch.setattr("pyharness.llm.client.STALL_TIMEOUT_S", 0.05)
    llm = _llm(monkeypatch, [HangingStream(), FakeStream(resp=_response("saved"))])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "saved"
    assert len(llm._client.messages.calls) == 2


def test_runaway_stream_hits_the_attempt_deadline(monkeypatch):
    monkeypatch.setattr("pyharness.llm.client.ATTEMPT_DEADLINE_S", 0.1)
    llm = _llm(monkeypatch, [BabblingStream(), FakeStream(resp=_response("saved"))])
    completion = llm.complete(messages=[{"role": "user", "content": "hi"}])
    assert completion.text == "saved"
    assert len(llm._client.messages.calls) == 2


def test_stream_stalled_is_retryable(monkeypatch):
    llm = _llm(monkeypatch, [])
    assert llm._retryable(StreamStalled("stalled"))
