from pathlib import Path

from pyharness.cli import _resolve_root, _trace


def test_llm_token_streams_inline_without_frame(capsys):
    _trace("llm_token", "hello")
    _trace("llm_token", " world")
    out = capsys.readouterr().out
    # Streamed verbatim, no box frame and no newline between chunks.
    assert out == "hello world"


def test_note_and_llm_call_are_suppressed(capsys):
    # Both carry text already shown via streamed llm_token chunks; rendering them
    # again is the double-render bug this suppression fixes.
    _trace("note", "some preamble")
    _trace("llm_call", "the whole answer")
    assert capsys.readouterr().out == ""


def test_code_and_output_still_boxed(capsys):
    _trace("code", "print(1)")
    _trace("output", "1")
    out = capsys.readouterr().out
    assert "┌─ python ─" in out and "print(1)" in out
    assert "┌─ output ─" in out and "1" in out


def test_resolve_root_prefers_cli_arg():
    # An explicit path wins over everything, even a configured persistent workspace.
    root = _resolve_root(["pyharness", "runs/foo"], {"PYHARNESS_WORKSPACE": "~/ws"}, "ts")
    assert root == Path("runs/foo")


def test_resolve_root_uses_persistent_workspace_env():
    # A stable workspace so dropped/created files survive across runs; ~ expands.
    root = _resolve_root(["pyharness"], {"PYHARNESS_WORKSPACE": "~/agent-home"}, "ts")
    assert root == Path("~/agent-home").expanduser()


def test_resolve_root_defaults_to_timestamped_session():
    root = _resolve_root(["pyharness"], {}, "20260101-000000")
    assert root == Path(".sessions/cli-20260101-000000")


def test_reflection_is_opt_in():
    from pyharness.cli import _reflect_enabled

    assert not _reflect_enabled({})  # off by default
    assert not _reflect_enabled({"PYHARNESS_REFLECT": "false"})
    assert not _reflect_enabled({"PYHARNESS_REFLECT": "nonsense"})
    assert _reflect_enabled({"PYHARNESS_REFLECT": "true"})
    assert _reflect_enabled({"PYHARNESS_REFLECT": " 1 "})


def test_llm_start_is_suppressed(capsys):
    from pyharness.cli import _trace

    _trace("llm_start", "")
    assert capsys.readouterr().out == ""
