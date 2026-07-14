from datetime import datetime, timezone

from pyharness import Budget
from pyharness.core.agent import Agent, render_context
from pyharness.core.kernel import Kernel
from pyharness.llm.client import Completion, ToolCall


class ScriptedLLM:
    """Stands in for the real client: returns pre-baked completions in order."""

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = []
        self.tiers = []
        self.systems = []

    def complete(self, *, system, messages, tier="smart", tools=None, max_tokens=None, on_token=None):
        self.calls.append(list(messages))
        self.tiers.append(tier)
        self.systems.append(system)
        return self.completions.pop(0)


def _tool_completion(code):
    call = ToolCall(id="t1", name="run_python", input={"code": code})
    return Completion(text="", tool_calls=[call], content=[{"k": "v"}], stop_reason="tool_use")


def _text_completion(text):
    return Completion(text=text, tool_calls=[], content=[{"k": "v"}], stop_reason="end_turn")


def test_agent_runs_code_then_answers(tmp_path):
    events = []
    llm = ScriptedLLM([
        _tool_completion("write('hello.txt', 'hi there')\nprint('written')"),
        _text_completion("Wrote the file."),
    ])
    from pyharness.broker import Broker
    from pyharness.broker.capabilities import FilesCapability
    from pyharness.audit import AuditLog
    from pyharness.security.policy import Policy
    from pyharness.core.workspace import Workspace

    ws = Workspace(tmp_path)
    broker = Broker(Policy(), AuditLog(tmp_path / "a.jsonl"), Budget())
    broker.register(FilesCapability(ws))
    kernel = Kernel(broker.namespace())
    agent = Agent(llm, kernel, Budget(), on_event=lambda k, t, **kw: events.append((k, t)))

    answer = agent.run("create hello.txt", [])

    assert answer == "Wrote the file."
    assert (ws.dir / "hello.txt").read_text() == "hi there"
    assert ("output", "written") in events


def test_agent_defaults_to_mid_tier():
    llm = ScriptedLLM([_text_completion("done")])
    agent = Agent(llm, Kernel({}), Budget())

    assert agent.run("answer", []) == "done"
    assert llm.tiers == ["mid"]


class FailingLLM:
    """Raises on complete() to simulate a stream that dies mid-turn."""

    def complete(self, *, system, messages, tier="smart", tools=None, max_tokens=None, on_token=None):
        raise RuntimeError("stream interrupted")


def test_aborted_turn_rolls_back_user_message(tmp_path):
    # A failed turn must leave history untouched, or the next send produces two
    # consecutive user turns and the API rejects every later message.
    messages = []
    agent = Agent(FailingLLM(), Kernel({}), Budget())

    try:
        agent.run("first task", messages)
    except RuntimeError:
        pass
    assert messages == []

    # History is clean, so a subsequent successful turn works normally.
    ok = Agent(ScriptedLLM([_text_completion("done")]), Kernel({}), Budget())
    answer = ok.run("second task", messages)
    assert answer == "done"
    assert messages[0] == {"role": "user", "content": "second task"}


def test_render_context_carries_date_platform_and_workspace():
    now = datetime(2026, 7, 13, 14, 30, tzinfo=timezone.utc)
    block = render_context("/tmp/ws", now=now)
    assert "2026-07-13 14:30" in block
    assert "Monday" in block
    assert "/tmp/ws" in block


def test_render_context_omits_workspace_when_unknown():
    block = render_context(None, now=datetime(2026, 7, 13, tzinfo=timezone.utc))
    assert "Workspace" not in block


def test_dynamic_context_is_appended_to_system_prompt(tmp_path):
    llm = ScriptedLLM([_text_completion("done")])
    agent = Agent(llm, Kernel({}), Budget(), workspace_root=tmp_path)

    agent.run("answer", [])

    system = llm.systems[0]
    assert "You are the orchestrator of pyharness." in system  # static contract
    assert "## Session" in system  # dynamic preamble
    assert str(tmp_path) in system


class _LookKernel:
    """A kernel whose cell attaches an image to the outbox, as browser.look would
    during real execution, then returns text output."""

    def __init__(self, outbox, data=b"\xff\xd8jpeg"):
        self.outbox = outbox
        self.data = data

    def run(self, code):
        self.outbox.attach(media_type="image/jpeg", data=self.data)
        return "looked"


def test_agent_attaches_image_blocks_from_outbox():
    import base64

    from pyharness.core.media import MediaOutbox

    outbox = MediaOutbox()
    llm = ScriptedLLM([_tool_completion("look()"), _text_completion("done")])
    agent = Agent(llm, _LookKernel(outbox), Budget(), media=outbox)

    agent.run("look at it", [])

    # The tool_result the second LLM call saw carries a text block + an image block.
    tool_msg = llm.calls[-1][-1]
    content = tool_msg["content"][0]["content"]
    assert content[0] == {"type": "text", "text": "looked"}
    assert content[1]["type"] == "image"
    assert base64.b64decode(content[1]["source"]["data"]) == b"\xff\xd8jpeg"


def test_agent_tool_result_is_plain_string_without_images():
    # No outbox, no images -> content stays a bare string exactly as before.
    llm = ScriptedLLM([_tool_completion("print('hi')"), _text_completion("done")])
    agent = Agent(llm, Kernel({}), Budget())

    agent.run("x", [])

    tool_msg = llm.calls[-1][-1]
    assert tool_msg["content"][0]["content"] == "hi"


def test_serialize_elides_nested_image_data():
    import json

    from pyharness.core.agent import _serialize_messages

    msgs = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t",
                    "content": [
                        {"type": "text", "text": "looked"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJDRA=="}},
                    ],
                }
            ],
        }
    ]
    dumped = json.dumps(_serialize_messages(msgs))
    assert "QUJDRA==" not in dumped  # base64 payload elided from the trace snapshot
    assert "image/jpeg" in dumped  # but the summary keeps the media type


def test_kernel_state_persists_across_cells(tmp_path):
    llm = ScriptedLLM([
        _tool_completion("n = 41"),
        _tool_completion("print(n + 1)"),
        _text_completion("done"),
    ])
    kernel = Kernel({})
    agent = Agent(llm, kernel, Budget())

    agent.run("compute", [])

    # The second cell's output reflects state from the first.
    last_user = llm.calls[-1][-1]
    assert last_user["content"][0]["content"] == "42"
