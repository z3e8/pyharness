"""Run the same task five times over one skills root, and read the curve back.

The measurement rests on three things, and dropping any of them turns the curve
into a number that always goes down:

**One task, one skills root, one index.** The runs differ only in what the skills
root already contains. Everything else the demo suite keeps hermetic is kept
hermetic here too — no shared skills dir, no global index, no MCP config from the
operator's checkout — because a cost curve that depends on which skills happen to
be installed on this laptop measures the laptop.

**A cheaper run must still be a correct run.** A run that answers from memory, or
answers wrong, is cheaper for a reason nobody wants. Every run is checked for two
markers that can only come from the two pages (the total, and an account number
that appears solely on the second hop), and separately for whether it actually
re-read the source. A run that got cheaper by skipping the fetch is reported as
such rather than counted as amortization.

**The curve comes out of the shipped view.** `skill_run_costs` in
`pyharness/obs/index.py` is what the agent itself can query with `stats()`; if
the suite computed its own per-run cost it would be grading a different artifact
than the one the harness publishes. The view only knows about sessions that
*recorded* a skill use, so the per-session table is reported next to it — a run
missing from the view is a fact about the mechanism, not a gap to paper over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..demo.runner import read_outbound
from ..demo.server import CorpusServer

# The task, verbatim and identical on every run. Two properties matter:
#
# - It is a *procedure*, not a lookup: two hops, an arithmetic cross-check and a
#   fixed output shape. A one-fetch task has no procedure to amortize, so a flat
#   curve on one would say nothing about skills.
# - It names the save/reuse cycle out loud. That is a deliberate choice and a
#   published caveat: this suite measures whether reuse is cheaper, not whether a
#   model spontaneously thinks to reuse. Left implicit, a run where the model
#   simply never saved a skill would produce a flat curve that reads as "skills
#   don't amortize" while having tested nothing — the same failure the demo
#   suite's first paid run made.
TASK = """\
Produce the Northwind billing summary for invoice INV-4471.

The invoice is at {url}; its remittance details are on a page linked from it.
Reply with exactly one line:

INV-4471 | line items <sum of the line-item totals> | due <amount due as \
printed> | pay <beneficiary>, account <account number>

The invoice page is the source of truth and may change between runs, so always
re-read it rather than answering from a previous run's numbers.

This same summary is requested every run. Before you start, search_tools() for a
saved skill that already does it: if there is one, follow it; if there is not,
save one once you have the answer — the URLs and the exact steps — so the next
run is cheaper. Either way, record_skill_use(name, "worked" or "failed") before
you reply.
"""

# Markers that can only come from the two corpus pages. The total is on the
# invoice; the account number appears solely on the remittance page, so a run
# that never took the second hop cannot produce it.
#
# The total is matched with a pattern rather than a literal, and the first paid
# run is why: three of five runs summed the line items in Python and reported the
# float, `1240.0`. Every one of them had fetched both pages and had the arithmetic
# right, and a literal `1240.00` failed them for the number of decimal places —
# grading formatting, which this check exists not to do. The trailing `\b` still
# rejects a wrong amount that happens to start with the same digits.
_TOTAL = re.compile(r"\b1240(\.\d+)?\b")
_ACCOUNT = "00000000"

PAGE = "invoice-benign.html"


def correct(answer: str) -> bool:
    """Whether an answer carries both markers. Thousands separators are stripped
    first: `1,240.00` and `1240.00` are the same answer, and scoring them
    differently would grade formatting."""
    normalized = answer.replace(",", "")
    return bool(_TOTAL.search(normalized)) and _ACCOUNT in normalized


@dataclass
class RunRow:
    """One run, as reported."""

    n: int
    name: str
    session_id: str
    outcome: str
    cost_usd: float
    steps: int
    llm_calls: int
    wall_s: float
    answer: str = ""
    fetches: int = 0  # outbound calls that reached the corpus origin
    saved: str = ""  # skill authored this run, if any
    reused: str = ""  # skill loaded from the registry this run, if any
    recorded: str = ""  # outcome recorded against a skill this run, if any
    run_n: int | None = None  # the view's own run number, None if absent from it

    @property
    def ok(self) -> bool:
        """A run whose number is usable: it finished, it re-read the source, and
        it produced both markers."""
        return self.outcome == "answered" and self.fetches > 0 and correct(self.answer)


def run_once(
    server: CorpusServer,
    *,
    root: Path,
    skills_dir: Path,
    index_db: Path,
    llm=None,
    budget_usd: float = 0.03,
    max_steps: int = 14,
    tier: str = "cheap",
) -> Path:
    """Run the task once and return the session dir.

    `llm=None` uses the real client at `tier` and costs money; the tests pass a
    scripted model, which is how every path here except the model call is
    exercised offline.

    Every approval is granted. `skills.save_skill` and `skills.edit_skill` are
    gated, so a denying approver would leave run 1 with nothing to reuse and the
    suite measuring nothing — this suite is about amortization, and the gate is
    measured by the demo suite instead.
    """
    from pyharness.broker.dispatch import ApprovalOutcome
    from pyharness.budget import Budget
    from pyharness.core.session import Session

    session = Session(
        root,
        llm=llm,
        budget=Budget(limit_usd=budget_usd),
        allowed_hosts=[server.host],
        approver=lambda request: ApprovalOutcome.ONCE,
        tier=tier,
        max_steps=max_steps,
        # The shared state under test, and nothing else shared: the skills root
        # and the index carry run 1's work into run 2, the way they would across
        # two real sessions.
        skills_dir=skills_dir,
        index_db=index_db,
        mcp_config=None,
    )
    try:
        session.run(TASK.format(url=server.url(PAGE)))
    except Exception:  # noqa: BLE001 — the digest below says what happened
        pass
    finally:
        session.close()
    return root


def _skill_events(session_dir: Path) -> tuple[str, str]:
    """`(saved, recorded)` — the skill this run authored and the one it recorded
    an outcome against, from the trace the skills capability writes."""
    from pyharness.obs.transcript import iter_jsonl

    saved = recorded = ""
    for entry in iter_jsonl(session_dir / "trace.jsonl"):
        kind = entry.get("kind")
        if kind in ("skill_saved", "skill_edited"):
            saved = str(entry.get("skill") or "")
        elif kind == "skill_use":
            recorded = f"{entry.get('skill')}:{entry.get('outcome')}"
    return saved, recorded


def _reused(session_dir: Path, names: set[str]) -> str:
    """The known skill this run pulled out of the registry, if any.

    Read from the audit chain rather than the model's text: `describe_tool` is
    what returns a learned skill's instructions and `use_tool` is what loads its
    code, so one of the two appearing with the skill's name is the reuse actually
    happening, not the agent saying it happened.
    """
    from pyharness.obs.transcript import iter_jsonl

    for entry in iter_jsonl(session_dir / "audit.jsonl"):
        if entry.get("action") not in ("tools.describe_tool", "tools.use_tool"):
            continue
        args = str(entry.get("args", ""))
        for name in names:
            if name and name in args:
                return name
    return ""


def _corpus_fetches(session_dir: Path, base: str = "") -> int:
    """Outbound calls that *succeeded* against the corpus origin this run — the
    evidence that the run re-read its source rather than answering from the
    skill's own text.

    `base` is empty when rescoring an old run, whose ephemeral port is long
    gone. That is safe rather than loose: the session is scoped to the corpus
    host, so a successful outbound call could not have reached anywhere else.
    """
    return sum(
        1
        for a in read_outbound(session_dir / "audit.jsonl")
        if a.ok and a.url.startswith(base)
    )


def curve(index_db: Path) -> list[dict]:
    """The shipped view, verbatim: one row per (skill, session) with its run
    number. This is the artifact the plan asked for and what `stats()` would
    return to the agent."""
    from pyharness.obs.index import query

    return query(
        index_db,
        "SELECT skill, session_id, started, cost_usd, outcome, run_n "
        "FROM skill_run_costs ORDER BY skill, run_n",
    )


def rescore(root: Path) -> tuple[list[RunRow], list[dict]]:
    """Re-derive the board from a finished run's own durable record.

    The suite's numbers are expensive and the scorer is not: a fix to how a run
    is judged should not need the runs made again, both because that costs money
    and because a second set of model calls would quietly change the numbers
    while claiming to be the same experiment. The session dirs, the audit chain
    and the index under `root` are enough to rebuild every row.
    """
    from pyharness.obs.index import query, update_index

    index_db = root / "index.db"
    update_index(index_db, [root], skills_dir=root / "skills")
    resolved = root.resolve()
    ordered = query(index_db, "SELECT id FROM sessions ORDER BY started", limit=1000)
    roots = [Path(r["id"]) for r in ordered if Path(r["id"]).parent == resolved]
    return gather(roots, index_db, base=""), curve(index_db)


def gather(roots: list[Path], index_db: Path, *, base: str = "") -> list[RunRow]:
    """Join the index's per-session numbers to the evidence on disk, in run
    order. `roots` are the session dirs in the order they were run."""
    from pyharness.obs.index import query

    rows: list[RunRow] = []
    indexed = {
        r["id"]: r
        for r in query(
            index_db,
            "SELECT id, name, outcome, cost_usd, steps, llm_calls, started, ended, "
            "answer FROM sessions",
            limit=1000,
        )
    }
    view = {r["session_id"]: r for r in curve(index_db)}
    saved_names = {_skill_events(root)[0] for root in roots} - {""}

    for n, root in enumerate(roots, start=1):
        sid = str(root.resolve())
        indexed_row = indexed.get(sid, {})
        saved, recorded = _skill_events(root)
        started = indexed_row.get("started") or 0.0
        ended = indexed_row.get("ended") or 0.0
        rows.append(
            RunRow(
                n=n,
                name=root.name,
                session_id=sid,
                outcome=str(indexed_row.get("outcome") or "missing"),
                cost_usd=float(indexed_row.get("cost_usd") or 0.0),
                steps=int(indexed_row.get("steps") or 0),
                llm_calls=int(indexed_row.get("llm_calls") or 0),
                wall_s=max(0.0, ended - started),
                answer=str(indexed_row.get("answer") or ""),
                fetches=_corpus_fetches(root, base),
                saved=saved,
                reused=_reused(root, saved_names),
                recorded=recorded,
                run_n=view.get(sid, {}).get("run_n"),
            )
        )
    return rows


@dataclass
class Curve:
    """The five rows plus the one comparison the suite exists to make."""

    rows: list[RunRow] = field(default_factory=list)

    @property
    def first(self) -> RunRow | None:
        return self.rows[0] if self.rows else None

    @property
    def later(self) -> list[RunRow]:
        return self.rows[1:]

    def mean(self, attr: str, rows: list[RunRow] | None = None) -> float:
        rows = self.rows if rows is None else rows
        return sum(getattr(r, attr) for r in rows) / len(rows) if rows else 0.0

    def delta(self, attr: str) -> float:
        """Runs 2-N against run 1, as a fraction. Negative means cheaper."""
        first = getattr(self.first, attr) if self.first else 0.0
        if not first or not self.later:
            return 0.0
        return self.mean(attr, self.later) / first - 1.0
