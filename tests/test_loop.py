from pyharness import Agent, Message, Workspace
from pyharness.agent import OutputKind, parse_output


class ScriptedLLM:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, list[Message]]] = []

    def complete(self, system: str, messages: list[Message]) -> str:
        self.calls.append((system, list(messages)))
        return self.replies.pop(0) if self.replies else "Done."


def test_code_action_loop(tmp_path):
    workspace = Workspace(tmp_path)
    llm = ScriptedLLM(
        [
            "```python\nwrite('hello.txt', 'hi there')\nprint('written')\n```",
            "Wrote the file.",
        ]
    )
    agent = Agent(llm, workspace)

    answer = agent.run("create hello.txt")

    assert answer == "Wrote the file."
    assert (workspace.dir / "hello.txt").read_text() == "hi there"


def test_namespace_persists_across_turns(tmp_path):
    workspace = Workspace(tmp_path)
    llm = ScriptedLLM(
        [
            "```python\nx = 41\n```",
            "```python\nprint(x + 1)\n```",
            "done",
        ]
    )
    agent = Agent(llm, workspace)

    agent.run("compute")

    # third call's feedback (last user message) reflects the persisted `x`
    assert llm.calls[-1][1][-1].content.strip() == "42"


def test_workspace_runs_shell_in_workspace(tmp_path):
    workspace = Workspace(tmp_path)

    assert workspace.bash("pwd").strip() == str(workspace.dir)


def test_parse_output_accepts_only_text_or_python():
    text = parse_output("All done.")
    code = parse_output("```python\nprint('hi')\n```")

    assert text is not None
    assert text.kind is OutputKind.TEXT
    assert text.content == "All done."
    assert code is not None
    assert code.kind is OutputKind.PYTHON
    assert code.content.strip() == "print('hi')"


def test_parse_output_rejects_mixed_or_non_python():
    assert parse_output("First:\n```python\nprint('hi')\n```") is None
    assert parse_output("```python\nprint('hi')\n```\nDone.") is None
    assert parse_output("```\nprint('hi')\n```") is None


def test_invalid_output_retries(tmp_path):
    workspace = Workspace(tmp_path)
    llm = ScriptedLLM(
        [
            "I'll run this:\n```python\nprint('bad')\n```",
            "All done.",
        ]
    )
    agent = Agent(llm, workspace)

    assert agent.run("answer") == "All done."
    assert llm.calls[-1][1][-1].content.startswith("Invalid output.")
