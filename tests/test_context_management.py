"""Context management in the agent loop: old cell outputs are elided (the
kernel still holds the variables, so nothing is lost), and each cell result
carries a one-line context/step/spend meter so the model sees its own
pressure instead of guessing."""

from pyharness.budget import Budget
from pyharness.core.agent import Agent, _elide_old_outputs, _elided
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
    assert messages[0] == {"role": "user", "content": "task"}  # plain messages untouched


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
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJDRA=="}},
    ]
    stub = _elided(content)
    assert isinstance(stub, str)
    assert "1 image" in stub


class MeteredLLM:
    """Returns one code cell then a text answer, with real-looking usage."""

    def __init__(self):
        usage = Usage(
            model="m", input_tokens=1200, output_tokens=50, cost_usd=0.01,
            cache_read_tokens=60_000, cache_creation_tokens=800,
        )
        self.completions = [
            Completion(
                text="", tool_calls=[ToolCall(id="t1", name="run_python", input={"code": "print('hi')"})],
                content=[{"k": "v"}], stop_reason="tool_use", usage=usage,
            ),
            Completion(text="done", tool_calls=[], content=[{"k": "v"}], stop_reason="end_turn", usage=usage),
        ]
        self.calls = []

    def complete(self, *, system, messages, tier="smart", tools=None, on_token=None, cache_anchor=None):
        self.calls.append([dict(m) for m in messages])
        return self.completions.pop(0)


def test_meter_line_lands_in_tool_result_only():
    events = []
    llm = MeteredLLM()
    budget = Budget(limit_usd=5.0)
    agent = Agent(llm, Kernel({}), budget, on_event=lambda k, t, **kw: events.append((k, t)))

    agent.run("task", [])

    content = llm.calls[-1][-1]["content"][0]["content"]
    assert content.startswith("hi\n[context: 62,000 tokens · step 1/30 · spent $0.00 of $5.00]")
    # The display/trace stream sees the raw output, no meter.
    assert ("output", "hi") in events


def test_usage_context_tokens_counts_cached_prompt():
    usage = Usage(
        model="m", input_tokens=100, output_tokens=1, cost_usd=0.0,
        cache_read_tokens=900, cache_creation_tokens=50,
    )
    assert usage.context_tokens == 1050
