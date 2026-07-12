from pyharness.cli import _trace


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
