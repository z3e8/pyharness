"""AnthropicLLM reliability: stream retries, cache markers, error classes.

All offline — the SDK client object is replaced with scripted fakes; only the
exception classes and request-shaping logic are real.
"""

from types import SimpleNamespace

import httpx
import pytest

import anthropic

from pyharness.llm.client import STREAM_ATTEMPTS, AnthropicLLM


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


class FakeStream:
    """One scripted stream attempt: yields `tokens`, then either raises `exc`
    (a mid-stream failure) or returns `resp` from get_final_message()."""

    def __init__(self, resp=None, exc=None, tokens=()):
        self.resp = resp
        self.exc = exc
        self.tokens = tokens

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        def gen():
            yield from self.tokens
            if self.exc is not None:
                raise self.exc
        return gen()

    def get_final_message(self):
        if self.exc is not None:
            raise self.exc
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

