"""Context management in the agent loop: old cell outputs are elided (the
kernel still holds the variables, so nothing is lost), and each cell result
carries a one-line context/step/spend meter so the model sees its own
pressure instead of guessing."""

from pyharness.budget import Budget
from pyharness.core.agent import (
    Agent,
    _cache_anchor,
    _elide_old_outputs,
    _elided,
    _evict_old_images,
)
from pyharness.core.kernel import Kernel
from pyharness.llm.client import Completion, ToolCall, Usage


def _tool_result_msg(*contents):
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": c}
            for i, c in enumerate(contents)
        ],
    }


def test_elides_only_beyond_the_recent_window():
    big = "x" * 2000
    messages = [
        {"role": "user", "content": "task"},
        _tool_result_msg(big),
        {"role": "assistant", "content": "next"},
        _tool_result_msg(big),
        _tool_result_msg(big),
    ]
    _elide_old_outputs(messages, keep_recent=2)

    assert messages[1]["content"][0]["content"].startswith("[output elided: 2000 chars")
    assert messages[3]["content"][0]["content"] == big  # inside the window
    assert messages[4]["content"][0]["content"] == big
    assert messages[0] == {
        "role": "user",
        "content": "task",
    }  # plain messages untouched


def test_small_outputs_survive_elision():
    messages = [_tool_result_msg("42"), _tool_result_msg("x" * 2000)]
    _elide_old_outputs(messages, keep_recent=1)
    assert messages[0]["content"][0]["content"] == "42"


def test_elision_is_idempotent_and_disableable():
    stub = _elided("x" * 2000)
    assert _elided(stub) == stub  # a stub is never re-stubbed

    big = "y" * 9000
    messages = [_tool_result_msg(big), _tool_result_msg(big)]
    _elide_old_outputs(messages, keep_recent=0)  # disabled
    assert messages[0]["content"][0]["content"] == big


def test_image_blocks_are_elided_even_when_text_is_small():
    content = [
        {"type": "text", "text": "looked"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "QUJDRA==",
            },
        },
    ]
    stub = _elided(content)
    assert isinstance(stub, str)
    assert "1 image" in stub


def _image_block():
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJDRA=="},
    }


def _look_msg(i, url=None):
    """A tool_result message as a look() cell leaves it: the kernel's echo of
    the returned state dict (carrying the url), then the image block."""
    text = f"{{'url': '{url}', 'title': 'T', 'attached': True}}" if url else "looked"
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": f"t{i}",
                "content": [{"type": "text", "text": text}, _image_block()],
            }
        ],
    }


def _has_image(msg):
    return any(
        isinstance(b, dict) and b.get("type") == "image"
        for b in msg["content"][0]["content"]
    )


def test_evicts_images_beyond_the_image_window_only():
    messages = [
        {"role": "user", "content": "task"},
        _look_msg(1, "https://a.example/one"),
        _tool_result_msg("plain text cell"),  # not image-carrying: not counted
        _look_msg(2, "https://b.example/two"),
        _look_msg(3, "https://c.example/three"),
        _look_msg(4, "https://d.example/four"),
    ]
    _evict_old_images(messages, keep_recent=2)

    # The two oldest image cells lost their images; the two newest kept them.
    assert not _has_image(messages[1]) and not _has_image(messages[3])
    assert _has_image(messages[4]) and _has_image(messages[5])
    # The note replaces the image *in place*: the cell's own text output stays
    # untouched at position 0, the note sits where the image block was.
    cell = messages[1]["content"][0]["content"]
    assert cell[0]["text"].startswith("{'url': 'https://a.example/one'")
    assert cell[1]["type"] == "text"
    # The text-only cell and the plain user message are untouched.
    assert messages[2]["content"][0]["content"] == "plain text cell"
    assert messages[0] == {"role": "user", "content": "task"}


def test_eviction_note_names_the_page_and_never_promises_recovery():
    messages = [_look_msg(1, "https://shop.example/cart"), _look_msg(2), _look_msg(3)]
    _evict_old_images(messages, keep_recent=1)

    with_url = messages[0]["content"][0]["content"][1]["text"]
    assert with_url.startswith("[screenshot dropped:")
    assert "https://shop.example/cart" in with_url  # which view was lost
    assert "look()" in with_url  # the honest path: a *fresh* capture
    assert "re-print" not in with_url and "re-read" not in with_url

    without_url = messages[1]["content"][0]["content"][1]["text"]
    assert without_url.startswith("[screenshot dropped:")
    assert "http" not in without_url


def test_keep_images_zero_disables_eviction():
    messages = [_look_msg(i) for i in range(5)]
    _evict_old_images(messages, keep_recent=0)
    assert all(_has_image(m) for m in messages)


def test_eviction_window_slides_over_cells_that_still_carry_images():
    # Statelessness: an evicted cell drops out of the selection, so the window
    # counts live image cells only — it never double-counts what it already ate.
    messages = [_look_msg(1), _look_msg(2), _look_msg(3)]
    _evict_old_images(messages, keep_recent=2)
    assert [_has_image(m) for m in messages] == [False, True, True]

    _evict_old_images(messages, keep_recent=2)  # idempotent with no new images
    assert [_has_image(m) for m in messages] == [False, True, True]

    messages.append(_look_msg(4))
    _evict_old_images(messages, keep_recent=2)
    assert [_has_image(m) for m in messages] == [False, False, True, True]


def test_cache_anchor_uses_the_eviction_frontier_when_elision_is_idle():
    messages = [
        {"role": "user", "content": "task"},
        _look_msg(1),
        _look_msg(2),
        _look_msg(3),
        _look_msg(4),
    ]
    # Nothing evicted yet: no frontier, full-history incremental caching.
    assert _cache_anchor(messages, 0, 2) is None

    _evict_old_images(messages, keep_recent=2)
    # Elision disabled: the anchor is the newest evicted cell (index 2).
    assert _cache_anchor(messages, 0, 2) == 2
    # Elision enabled but not started (window of 8 > 4 cells): same frontier.
    assert _cache_anchor(messages, 8, 2) == 2
    # Eviction disabled: the marker means nothing, back to the old behavior.
    assert _cache_anchor(messages, 0, 0) is None


def test_cache_anchor_prefers_the_elision_frontier_once_it_exists():
    # Eight look cells, image window 1, elision window 2: eviction's newest
    # note (index 7) sits *above* the elision frontier (index 6). The elision
    # frontier must still win — cells past it are not in final form (their text
    # elides later), so anchoring on the newer eviction note would break the
    # cache when elision reaches it.
    messages = [{"role": "user", "content": "task"}]
    messages += [_look_msg(i) for i in range(1, 9)]
    _evict_old_images(messages, keep_recent=1)
    _elide_old_outputs(messages, keep_recent=2)

    assert _cache_anchor(messages, 2, 1) == 6


class MeteredLLM:
    """Returns one code cell then a text answer, with real-looking usage."""

    def __init__(self):
        usage = Usage(
            model="m",
            input_tokens=1200,
            output_tokens=50,
            cost_usd=0.01,
            cache_read_tokens=60_000,
            cache_creation_tokens=800,
        )
        self.completions = [
            Completion(
                text="",
                tool_calls=[
                    ToolCall(id="t1", name="run_python", input={"code": "print('hi')"})
                ],
                content=[{"k": "v"}],
                stop_reason="tool_use",
                usage=usage,
            ),
            Completion(
                text="done",
                tool_calls=[],
                content=[{"k": "v"}],
                stop_reason="end_turn",
                usage=usage,
            ),
        ]
        self.calls = []

    def complete(
        self,
        *,
        system,
        messages,
        tier="smart",
        tools=None,
        on_token=None,
        on_thinking=None,
        on_attempt=None,
        cache_anchor=None,
        total_deadline_s=None,
    ):
        self.calls.append([dict(m) for m in messages])
        return self.completions.pop(0)


def test_meter_line_lands_in_tool_result_only():
    events = []
    llm = MeteredLLM()
    budget = Budget(limit_usd=5.0)
    agent = Agent(
        llm, Kernel({}), budget, on_event=lambda k, t, **kw: events.append((k, t))
    )

    agent.run("task", [])

    content = llm.calls[-1][-1]["content"][0]["content"]
    assert content.startswith(
        "hi\n[context: 62,000 tokens · step 1/30 · spent $0.00 of $5.00]"
    )
    # The display/trace stream sees the raw output, no meter.
    assert ("output", "hi") in events


def test_usage_context_tokens_counts_cached_prompt():
    usage = Usage(
        model="m",
        input_tokens=100,
        output_tokens=1,
        cost_usd=0.0,
        cache_read_tokens=900,
        cache_creation_tokens=50,
    )
    assert usage.context_tokens == 1050
