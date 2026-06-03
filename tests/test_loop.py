from pyharness import (
    FakeProvider,
    Master,
    PermissionDenied,
    RulePolicy,
    Session,
    Toolbox,
)
from pyharness.agent import extract_code


def test_code_action_loop(tmp_path):
    session = Session(tmp_path)
    provider = FakeProvider(
        [
            "```python\nwrite('hello.txt', 'hi there')\nprint('written')\n```",
            "Wrote the file.",
        ]
    )
    policy = RulePolicy(allow=["file.write:*"])
    master = Master(provider, session, policy)

    answer = master.run("create hello.txt")

    assert answer == "Wrote the file."
    assert (session.workspace / "hello.txt").read_text() == "hi there"


def test_namespace_persists_across_turns(tmp_path):
    session = Session(tmp_path)
    provider = FakeProvider(
        [
            "```python\nx = 41\n```",
            "```python\nprint(x + 1)\n```",
            "done",
        ]
    )
    master = Master(provider, session, RulePolicy.allow_all())

    master.run("compute")

    # third call's feedback (last user message) reflects the persisted `x`
    assert provider.calls[-1][1][-1].content.strip() == "42"


def test_permission_denied(tmp_path):
    session = Session(tmp_path)
    tb = Toolbox(session, RulePolicy(allow=[]), FakeProvider([]))
    try:
        tb.bash("echo hi")
        assert False, "expected PermissionDenied"
    except PermissionDenied:
        pass


def test_extract_code():
    assert extract_code("text only") is None
    assert extract_code("```python\nx=1\n```").strip() == "x=1"
