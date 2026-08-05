"""Offline coverage for the throughput suite.

The board costs a model call in each of three arms, so everything that is not
the model call is driven here with `support.ScriptedLLM` — the real agent loop,
the real kernel, the real workspace boundary, and the real corpus, with the
agent's code chosen rather than hoped for.

Four things this has to establish, and the last two are the ones that matter:

1. **The corpus is reproducible.** Same seed, same bytes. Otherwise "regenerate
   it from a seed" is not a substitute for committing 50MB.
2. **The corpus discriminates.** Every naive shortcut still yields a different
   answer from the correct one. A corpus that has lost this measures nothing
   while still printing green.
3. **The scorer can fail.** A naive answer must score NAIVE and a wrong one must
   score wrong. A scorer that says "correct" no matter what has measured
   nothing — the analogue of `Attack.control` in the adversarial suite.
4. **The answer key is out of reach.** Not by permission — by absence. It is
   never on disk while an arm is running. The first design wrote it to the
   session root and trusted the workspace boundary to hold; the test below
   showed the agent reading it through `../../truth.json`, because that boundary
   governs the broker's file capability and not `open()` in the kernel. Every
   score in this suite would have been worthless and nothing else here would
   have noticed.

Without 2-4, 1 is bookkeeping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .data import baseline
from .data.gen import (
    DUPLICATE_DAY,
    REGRESSION_DAY,
    UNITS_DAY,
    derive,
    generate,
    read_rows,
    verify,
)
from .data.runner import give_corpus, run_brokered, save_truth, stage_corpus
from .data.tasks import judge, parse_answer, prompt
from .support import ScriptedLLM, ScriptedToolLLM

# Small enough to be free, large enough that the peak-hour discrimination is not
# a coin flip: it holds from ~600 rows/day, and this is double that.
ROWS = 1200


@pytest.fixture
def corpus(tmp_path) -> Path:
    generate(tmp_path / "logs", rows_per_day=ROWS)
    return tmp_path / "logs"


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------


def test_same_seed_produces_identical_bytes(tmp_path):
    """The whole reason the corpus is not committed.

    gzip stores an mtime in its header, so this fails without `mtime=0` even
    though every row is identical — and it would fail as a silent drift rather
    than as an error.
    """
    generate(tmp_path / "a", rows_per_day=ROWS)
    generate(tmp_path / "b", rows_per_day=ROWS)
    for left in sorted((tmp_path / "a").glob("*.gz")):
        right = tmp_path / "b" / left.name
        assert left.read_bytes() == right.read_bytes(), f"{left.name} is not stable"


def test_a_different_seed_produces_a_different_corpus(tmp_path):
    """Guards the opposite mistake: a generator that ignores its seed would pass
    the reproducibility test perfectly."""
    generate(tmp_path / "a", rows_per_day=ROWS, seed=1)
    generate(tmp_path / "b", rows_per_day=ROWS, seed=2)
    assert (tmp_path / "a" / "2026-06-01.ndjson.gz").read_bytes() != (
        tmp_path / "b" / "2026-06-01.ndjson.gz"
    ).read_bytes()


def test_the_corpus_still_discriminates(corpus):
    """Every question must have a reachable wrong answer that differs from the
    right one. This is the assertion that stops the suite from quietly becoming
    a formality after someone tunes a constant."""
    verify(derive(corpus))


def test_truth_is_derived_from_the_bytes_not_from_intent(corpus):
    """Deriving twice from the same directory must agree, and must agree with
    what `generate` returned — the answer key and the artifact cannot drift."""
    first = generate(corpus, rows_per_day=ROWS)
    assert derive(corpus).as_dict()["requests"] == first.requests
    assert derive(corpus).as_dict()["breach_day"] == first.breach_day


def test_every_planted_defect_is_actually_on_disk(corpus):
    """A defect that failed to land would make its question trivially correct
    and nothing else here would fail."""
    rows, malformed = read_rows(corpus)

    # field-rename: the key changes partway through the month
    assert any("route" in row for row in rows), "no row uses the original key"
    assert any("path" in row for row in rows), "the rename never happened"

    # duplicate-export: one day's ids appear twice
    day = [r for r in rows if r["_day"] == DUPLICATE_DAY]
    assert len(day) == 2 * len({r["request_id"] for r in day})

    # unit-change: the stored numbers jump three orders of magnitude
    before = [r["latency_ms"] for r in rows if r["_day"] == UNITS_DAY - 1]
    after = [r["latency_ms"] for r in rows if r["_day"] == UNITS_DAY]
    assert max(after) > 100 * max(before)

    # malformed-lines: counted across the month, not on a chosen day. They are
    # scattered at random, so asserting on day 1 tested the seed rather than the
    # generator — and passed or failed depending on it.
    assert malformed > 0, "no truncated lines landed anywhere in the month"
    assert malformed == derive(corpus).malformed

    # timezone-shift is asserted through the answer it changes, in `verify`.


def test_the_regression_is_the_real_answer_not_an_artifact(corpus):
    """The suite's headline number. If the units change and the regression ever
    collapsed onto the same day, the second question would stop separating an
    agent that read its data from one that did not."""
    truth = derive(corpus)
    assert truth.breach_day == REGRESSION_DAY
    assert truth.naive_breach_day == UNITS_DAY
    assert truth.breach_day != truth.naive_breach_day


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------


def test_the_correct_answer_scores_three_out_of_three(corpus):
    truth = derive(corpus)
    verdict = judge(
        f"requests={truth.requests}\nbreach_day={truth.breach_day}\n"
        f"peak_hour={truth.peak_hour}",
        truth,
    )
    assert verdict.score == 3
    assert not verdict.naive and not verdict.missing


def test_the_naive_answer_scores_zero_and_is_named_as_naive(corpus):
    """The control. A scorer that cannot tell the shortcut from the answer would
    make every result in this suite meaningless."""
    truth = derive(corpus)
    verdict = judge(
        f"requests={truth.naive_requests}\nbreach_day={truth.naive_breach_day}\n"
        f"peak_hour={truth.naive_peak_hour}",
        truth,
    )
    assert verdict.score == 0
    assert set(verdict.naive) == {"requests", "breach_day", "peak_hour"}


def test_a_merely_wrong_answer_is_not_reported_as_naive(corpus):
    """Wrong and naive are different findings: one says the arm was eaten by a
    known defect, the other says something else went on."""
    truth = derive(corpus)
    verdict = judge("requests=1\nbreach_day=2\npeak_hour=3", truth)
    assert verdict.score == 0
    assert not verdict.naive


def test_a_missing_answer_is_missing_rather_than_wrong(corpus):
    verdict = judge("I was unable to process the files.", derive(corpus))
    assert set(verdict.missing) == {"requests", "breach_day", "peak_hour"}


def test_defects_are_derived_from_the_integers_not_from_the_prose(corpus):
    """A model that describes a timezone problem and still reports the wrong
    hour has not handled it."""
    truth = derive(corpus)
    claimed = judge(
        "I corrected for a timezone shift and a duplicated export.\n"
        f"requests={truth.naive_requests}\nbreach_day={truth.breach_day}\n"
        f"peak_hour={truth.naive_peak_hour}",
        truth,
    )
    assert claimed.defects_neutralised(truth) == ("field-rename", "unit-change")


def test_the_parser_takes_the_conclusion_not_the_working(corpus):
    """Models restate numbers while reasoning. Scoring the first mention would
    measure tidiness."""
    parsed = parse_answer(
        "First I got requests=999, but that double-counted.\n\n"
        "requests=298,603\nbreach_day=25\npeak_hour=15"
    )
    assert parsed == {"requests": 298603, "breach_day": 25, "peak_hour": 15}


# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------


def test_the_prompt_never_names_a_defect():
    """The task is to notice. A prompt that leaks a day, a field or a unit would
    make every correct answer unattributable."""
    text = prompt().lower()
    for leak in (
        "timezone",
        "utc-4",
        "duplicate",
        "microsecond",
        "rename",
        "malformed",
    ):
        assert leak not in text, f"the prompt gives away {leak!r}"


def test_the_brokered_arm_runs_the_real_harness_end_to_end(tmp_path):
    """A scripted agent that does the work correctly must score 3/3 through the
    real session, kernel and workspace. This is the path a live model takes."""
    truth = stage_corpus(tmp_path, rows_per_day=ROWS)
    root = tmp_path / "brokered"
    give_corpus(tmp_path, root / "workspace" / "logs")
    run = run_brokered(
        root,
        truth,
        llm=ScriptedLLM(
            "import gzip, json, glob\n"
            "rows=[]\n"
            "for p in sorted(glob.glob('logs/*.ndjson.gz')):\n"
            "    day=int(p.split('-')[2][:2])\n"
            "    for line in gzip.open(p,'rt'):\n"
            "        try: r=json.loads(line)\n"
            "        except Exception: continue\n"
            "        r['_day']=day; rows.append(r)\n"
            "print(len({r['request_id'] for r in rows}))",
            answer=(
                f"requests={truth.requests}\nbreach_day={truth.breach_day}\n"
                f"peak_hour={truth.peak_hour}"
            ),
        ),
    )
    assert not run.broke, run.broke
    assert run.score == 3
    assert run.steps >= 1, "the session recorded no code step"


def test_the_answer_key_is_not_on_disk_while_an_arm_runs(tmp_path):
    """The load-bearing isolation, and the one that was wrong first.

    The original design wrote the key to the session root and trusted
    `Workspace.path` to keep the agent inside `workspace/`. It does not: that
    guard covers the broker's file capability, while code in the kernel calls
    `open()` directly, and `../../truth.json` read the key. Every score in the
    suite would have been worthless and nothing else here would have noticed.

    So the property is now the absolute one — the key exists nowhere on disk
    until the arms are done — and this test walks the whole tree to say so
    rather than trusting any sandbox to hold.
    """
    truth = stage_corpus(tmp_path, rows_per_day=ROWS)
    root = tmp_path / "brokered"
    give_corpus(tmp_path, root / "workspace" / "logs")

    run = run_brokered(
        root,
        truth,
        llm=ScriptedLLM(
            # Exactly the escape that used to work: up out of `workspace/`, up
            # out of the session root, into the directory that held the key.
            "from pathlib import Path\n"
            "found = sorted(str(p) for p in Path.cwd().parent.parent"
            ".rglob('truth.json'))\n"
            "print('KEYFILES', found)",
            answer="requests=1\nbreach_day=1\npeak_hour=1",
        ),
    )
    assert not run.broke, run.broke
    trace = (root / "trace.jsonl").read_text()
    assert "KEYFILES []" in trace, (
        "the probe did not run, or it found a key file: the escape path is "
        f"still open — {trace[-400:]}"
    )

    # Nothing anywhere under the run's tree holds the answer, by inspection
    # rather than by permission.
    needle = str(truth.requests)
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix in ("", ".json", ".jsonl", ".txt"):
            assert needle not in path.read_text(errors="replace"), (
                f"the answer is readable in {path.relative_to(tmp_path)}"
            )
    assert run.score == 0


def test_the_key_is_written_only_after_the_arms_finish(tmp_path):
    """`save_truth` is the other half of the property above: the key does become
    an artifact, just never while anything is running."""
    truth = stage_corpus(tmp_path, rows_per_day=ROWS)
    assert not (tmp_path / "truth.json").exists()
    save_truth(tmp_path, truth)
    written = json.loads((tmp_path / "truth.json").read_text())
    assert written["requests"] == truth.requests


@pytest.mark.parametrize("arm", ["files", "shell"])
def test_the_control_arms_have_working_tools(tmp_path, arm):
    """A control arm that loses because its tools are broken measures nothing.
    Both must be able to reach real rows."""
    stage_corpus(tmp_path, rows_per_day=ROWS)
    data = give_corpus(tmp_path, tmp_path / f"arm-{arm}" / "logs")

    listing = baseline._call_tool(arm, "list_files", {}, data)
    assert "2026-06-01.ndjson.gz" in listing

    if arm == "files":
        out = baseline._call_tool(
            arm, "read_file", {"path": "2026-06-01.ndjson.gz", "max_lines": 3}, data
        )
    else:
        out = baseline._call_tool(
            arm, "shell", {"command": "gzip -dc 2026-06-01.ndjson.gz | head -3"}, data
        )
    assert json.loads(out.splitlines()[0])["service"] == "edge", out[:200]


def test_a_control_arm_records_what_it_did(tmp_path):
    """The control arms are not sessions, so if the loop does not record itself
    nothing else will. Without this the board's two control rows are numbers
    with nothing behind them, and the comparison cannot be read — only trusted.
    """
    truth = stage_corpus(tmp_path, rows_per_day=ROWS)
    data = give_corpus(tmp_path, tmp_path / "arm-files" / "logs")
    run = baseline.run_baseline(
        "files",
        data,
        truth,
        client=ScriptedToolLLM(
            ("list_files", {}),
            ("read_file", {"path": "2026-06-01.ndjson.gz"}),
            answer="requests=1\nbreach_day=2\npeak_hour=3",
        ),
    )
    assert [entry["tool"] for entry in run.transcript] == ["list_files", "read_file"]
    assert run.steps == 2

    page = baseline.render_transcript(run, truth)
    assert "Control arm: `files`" in page
    assert "list_files" in page and "read_file" in page
    # The real output was clipped for the artifact; the page has to say so
    # rather than quietly presenting a slice as the whole thing.
    assert "chars total]" in page
    assert str(truth.requests) in page, "the page should state the answer key"


def test_the_file_arm_cannot_escape_its_data_directory(tmp_path):
    stage_corpus(tmp_path, rows_per_day=ROWS)
    data = give_corpus(tmp_path, tmp_path / "arm-files" / "logs")
    out = baseline._call_tool("files", "read_file", {"path": "../../truth.json"}, data)
    assert out.startswith("error:")


def test_the_shell_arm_refuses_destructive_commands_only(tmp_path):
    """The seatbelt has to stop the thing that could wreck the developer's
    machine without touching anything the task needs — otherwise it is a policy,
    and the arm stops being a no-broker control."""
    stage_corpus(tmp_path, rows_per_day=ROWS)
    data = give_corpus(tmp_path, tmp_path / "arm-shell" / "logs")
    assert baseline._call_tool(
        "shell", "shell", {"command": "rm -rf /"}, data
    ).startswith("error: refused")
    for allowed in (
        "gzip -dc 2026-06-01.ndjson.gz | wc -l",
        "python3 -c \"import gzip,json;print('ok')\"",
        "ls *.gz | wc -l",
    ):
        assert not baseline._call_tool(
            "shell", "shell", {"command": allowed}, data
        ).startswith("error: refused"), allowed
