from pyharness import Budget
from pyharness.core.agent import Agent
from pyharness.core.kernel import Kernel
from pyharness.llm.client import Completion, ToolCall


class ScriptedLLM:
    """Stands in for the real client: returns pre-baked completions in order."""

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = []

    def complete(self, *, system, messages, tier="smart", tools=None, max_tokens=None, on_token=None):
        self.calls.append(list(messages))
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
