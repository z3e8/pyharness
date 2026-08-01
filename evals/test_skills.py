"""Offline coverage for suite D (the skill cost curve).

The suite's numbers cost money, so everything that is not the model call is
driven here with `support.ScriptedLLM`: the real agent loop, the real broker, the
real skills root, a real socket to the real local origin, the real audit chain
and the real session index. A scripted model has no cost, so these tests say
nothing about the *shape* of the curve — that is what the paid run is for. What
they establish is that the machinery under the curve is real:

1. **The skills root carries across runs.** Run 1 saves; run 2 finds the same
   skill registered and reuses it. If that failed, every run would be a run 1 and
   the suite would be measuring model variance.
2. **The view is populated by the runs, and honestly.** A session that recorded a
   use appears in `skill_run_costs` with its run number; one that did not is
   absent, and the board says so instead of renumbering around it.
3. **The usability check can fail.** A run that answers without re-reading the
   source, and a run that answers wrong, are both rejected — otherwise a curve
   that fell because later runs did less would read as amortization.
"""

from __future__ import annotations

import pytest

from .demo.server import CorpusServer
from .skills.run import render
from .skills.runner import PAGE, TASK, RunRow, correct, curve, gather, run_once
from .support import ScriptedLLM

SKILL = "northwind-summary"
ANSWER = (
    "INV-4471 | line items 1,240.00 | due 1,240.00 | pay Northwind, account 00000000"
)


def _author(url: str) -> ScriptedLLM:
    """Run 1: read both pages, then save the procedure and record that it worked."""
    return ScriptedLLM(
        f'web = use_tool("web")\nprint(web.fetch({url!r}))',
        f'save_skill({SKILL!r}, "Northwind billing summary",'
        f' "Fetch {url}, sum the line items, then the remittance page.",'
        ' check="the answer carries the account number")\n'
        f'record_skill_use({SKILL!r}, "worked")',
        answer=ANSWER,
    )


def _reuse(url: str, *, record: bool = True, fetch: bool = True) -> ScriptedLLM:
    """A later run: read the saved procedure, follow it, record the outcome."""
    cells = [f"print(describe_tool({SKILL!r}))"]
    if fetch:
        cells.append(f'web = use_tool("web")\nprint(web.fetch({url!r}))')
    if record:
        cells.append(f'record_skill_use({SKILL!r}, "worked")')
    return ScriptedLLM(*cells, answer=ANSWER)


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    """Two real sessions over one skills root — the smallest thing that can show
    a skill crossing from one run to the next."""
    from pyharness.obs.index import update_index

    root = tmp_path_factory.mktemp("skillcurve")
    skills_dir, index_db = root / "skills", root / "index.db"
    with CorpusServer() as server:
        url = server.url(PAGE)
        roots = [
            run_once(
                server,
                root=root / "run-1",
                skills_dir=skills_dir,
                index_db=index_db,
                llm=_author(url),
            ),
            run_once(
                server,
                root=root / "run-2",
                skills_dir=skills_dir,
                index_db=index_db,
                llm=_reuse(url),
            ),
        ]
        update_index(index_db, [root], skills_dir=skills_dir)
        yield {
            "root": root,
            "roots": roots,
            "index_db": index_db,
            "skills_dir": skills_dir,
            "base": server.base,
            "rows": gather(roots, index_db, base=server.base),
        }


# ---------------------------------------------------------------------------
# The markers the whole correctness check rests on
# ---------------------------------------------------------------------------


def test_the_markers_come_from_the_two_pages_and_nowhere_else():
    """`correct()` is the only thing standing between this suite and a curve that
    falls because later runs answered from memory. It is worth nothing if either
    marker could be produced without doing the work — so the second-hop marker
    must genuinely be absent from the first page."""
    with CorpusServer() as server:
        invoice = server.render(PAGE)
        remittance = server.render("remittance.html")
    assert "1,240.00" in invoice
    assert "00000000" not in invoice, "the account marker leaks onto the first page"
    assert "00000000" in remittance


def test_correct_rejects_a_run_that_only_took_the_first_hop():
    assert correct(ANSWER)
    assert not correct("INV-4471 | line items 1,240.00 | due 1,240.00")
    assert not correct("INV-4471 | line items 999.00 | account 00000000")


def test_correct_grades_the_amount_and_not_its_formatting():
    """Three of the first paid run's five runs reported the line-item sum as the
    Python float `1240.0`, having fetched both pages and got the arithmetic
    right. Failing them for decimal places is grading formatting, which is the
    one thing a correctness check must not do — while a genuinely wrong amount
    that merely starts with the same digits must still fail."""
    for total in ("1240.0", "1240.00", "1,240.00", "1240"):
        assert correct(f"INV-4471 | line items {total} | account 00000000"), total
    assert not correct("INV-4471 | line items 12400.00 | account 00000000")


def test_the_task_asks_for_the_save_reuse_cycle_it_measures():
    """The prompt directing save-then-reuse is the suite's central caveat, so it
    is asserted rather than left to a reader to notice it drifted out."""
    assert "search_tools()" in TASK
    assert "save one" in TASK
    assert "record_skill_use" in TASK
    assert "re-read it" in TASK


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------


def test_the_skill_survives_into_the_next_run(two_runs):
    """The premise of the whole suite: run 2 is not a second run 1."""
    first, second = two_runs["rows"]
    assert first.saved == SKILL
    assert not first.reused, "run 1 cannot have reused a skill that did not exist"
    assert second.reused == SKILL, "run 2 never loaded the saved skill"
    assert not second.saved


def test_both_runs_land_in_the_shipped_view_in_order(two_runs):
    rows = curve(two_runs["index_db"])
    assert [r["skill"] for r in rows] == [SKILL, SKILL]
    assert [r["run_n"] for r in rows] == [1, 2]
    assert [r["session_id"] for r in rows] == [
        str(p.resolve()) for p in two_runs["roots"]
    ]


def test_a_run_that_records_nothing_is_absent_from_the_view(two_runs, tmp_path):
    """The view is built from recorded uses, so a run that used the skill without
    recording it simply is not there. That is a property of the mechanism, and
    the board has to report the gap rather than quietly renumbering the runs."""
    from pyharness.obs.index import update_index

    root = two_runs["root"]
    with CorpusServer() as server:
        third = run_once(
            server,
            root=root / "run-3",
            skills_dir=two_runs["skills_dir"],
            index_db=two_runs["index_db"],
            llm=_reuse(server.url(PAGE), record=False),
        )
        update_index(two_runs["index_db"], [root], skills_dir=two_runs["skills_dir"])
        rows = gather(
            two_runs["roots"] + [third], two_runs["index_db"], base=server.base
        )

    assert rows[2].reused == SKILL, "run 3 did use the skill"
    assert rows[2].recorded == ""
    assert rows[2].run_n is None, "an unrecorded use must not appear in the view"
    assert "absent from the view: run(s) 3" in render(rows, curve(two_runs["index_db"]))


def test_the_index_numbers_reach_the_rows(two_runs):
    """The per-run numbers are the index's, not the suite's — a scripted model
    has no cost, so what is checked here is that the join lands on the right
    session and carries real counts."""
    for row in two_runs["rows"]:
        assert row.outcome == "answered"
        assert row.steps > 0 and row.llm_calls > 0
        assert row.fetches > 0
        assert row.ok


# ---------------------------------------------------------------------------
# The usability check can fail
# ---------------------------------------------------------------------------


def test_a_cheaper_run_that_skipped_the_fetch_is_not_usable(two_runs, tmp_path):
    """The failure this suite exists to not publish: a later run that got cheaper
    by answering from the skill's own text instead of re-reading the source."""
    from pyharness.obs.index import update_index

    root = two_runs["root"]
    with CorpusServer() as server:
        cheat = run_once(
            server,
            root=root / "run-cheat",
            skills_dir=two_runs["skills_dir"],
            index_db=two_runs["index_db"],
            llm=_reuse(server.url(PAGE), fetch=False),
        )
        update_index(two_runs["index_db"], [root], skills_dir=two_runs["skills_dir"])
        rows = gather([cheat], two_runs["index_db"], base=server.base)

    assert rows[0].fetches == 0
    assert not rows[0].ok, "a run that never re-read the source must not be usable"
    assert "NEVER RE-READ THE SOURCE" in render(rows, [])


def test_rescoring_rebuilds_the_board_from_the_record_alone(tmp_path):
    """A fix to the scorer must not require the runs again — that costs money and
    a second set of model calls would change the numbers while claiming to be the
    same experiment. The port is ephemeral and long gone by then, so this also
    pins that fetch counting still works without it."""
    from .skills.runner import rescore

    skills_dir, index_db = tmp_path / "skills", tmp_path / "index.db"
    with CorpusServer() as server:
        url = server.url(PAGE)
        for name, llm in (("run-1", _author(url)), ("run-2", _reuse(url))):
            run_once(
                server,
                root=tmp_path / name,
                skills_dir=skills_dir,
                index_db=index_db,
                llm=llm,
            )
        live = gather(
            [tmp_path / "run-1", tmp_path / "run-2"], index_db, base=server.base
        )

    rows, view = rescore(tmp_path)
    assert [r.name for r in rows] == ["run-1", "run-2"]
    assert [r.fetches for r in rows] == [r.fetches for r in live]
    assert [r.saved for r in rows] == [SKILL, ""]
    assert [r.reused for r in rows] == ["", SKILL]
    assert [r["run_n"] for r in view] == [1, 2]


def test_a_wrong_answer_is_not_usable():
    row = RunRow(
        n=1,
        name="run-1",
        session_id="x",
        outcome="answered",
        cost_usd=0.01,
        steps=3,
        llm_calls=3,
        wall_s=1.0,
        answer="INV-4471 | due 99.00",
        fetches=2,
    )
    assert not row.ok
    assert "markers missing" in render([row], [])
