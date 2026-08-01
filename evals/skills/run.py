"""Run suite D — the skill cost curve.

    python -m evals.skills.run                 # five runs (needs an API key)
    python -m evals.skills.run --runs 3        # fewer, for a cheaper probe
    python -m evals.skills.run --write out.md  # also write the board as an artifact

Exit code is 0 when every run produced a usable number and 1 otherwise. "Usable"
is deliberately strict — answered, re-read the source, both markers present —
because the failure mode of a cost curve is a later run that got cheaper by doing
less, and that must be a red suite rather than a nice-looking slope.

The curve itself is never a pass/fail. Whether repetition amortizes is the
question being asked; pinning the exit code to the answer would mean the suite
could only ever report the answer it was built to want.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from ..demo.server import CorpusServer
from .runner import Curve, RunRow, curve, gather, rescore, run_once


def _skill_cell(row: RunRow) -> str:
    bits = []
    if row.saved:
        bits.append(f"saved {row.saved}")
    if row.reused:
        bits.append(f"reused {row.reused}")
    if row.recorded:
        bits.append(f"recorded {row.recorded}")
    return ", ".join(bits) or "no skill"


def _answer_cell(row: RunRow) -> str:
    if row.ok:
        return f"ok, {row.fetches} fetch{'es' * (row.fetches != 1)}"
    faults = []
    if row.outcome != "answered":
        faults.append(row.outcome)
    if not row.fetches:
        faults.append("NEVER RE-READ THE SOURCE")
    from .runner import correct

    if not correct(row.answer):
        faults.append("markers missing")
    return "; ".join(faults)


def render(rows: list[RunRow], view: list[dict]) -> str:
    """The board.

    Cost, steps and wall time are printed per run rather than averaged into a
    headline. n=5 supports a shape, not a statistic, and a single "-57%" with no
    per-run column invites a reader to treat five samples as a rate.
    """
    c = Curve(rows)
    total = sum(r.cost_usd for r in rows)
    bad = [r for r in rows if not r.ok]
    lines = [
        "",
        f"  skill cost curve — {len(rows)} runs of one task (${total:.4f})",
        "",
        "    run  outcome        cost      steps  llm  wall     skill / answer",
    ]
    for row in rows:
        flag = " " if row.ok else "!"
        lines.append(
            f"   {flag}{row.n:<4} {row.outcome:<14} ${row.cost_usd:<8.4f} "
            f"{row.steps:<6} {row.llm_calls:<4} {row.wall_s:>6.1f}s  "
            f"{_skill_cell(row)}"
        )
        lines.append(
            f"        {'':<14} {'':<9} {'':<6} {'':<4} {'':>7}  {_answer_cell(row)}"
        )
    if c.first and c.later:
        lines += [
            "",
            f"    run 1        ${c.first.cost_usd:.4f}, {c.first.steps} steps, "
            f"{c.first.wall_s:.1f}s",
            f"    runs 2-{len(rows)}      ${c.mean('cost_usd', c.later):.4f} mean, "
            f"{c.mean('steps', c.later):.1f} steps, "
            f"{c.mean('wall_s', c.later):.1f}s",
            f"    delta        cost {c.delta('cost_usd'):+.0%}, "
            f"steps {c.delta('steps'):+.0%}, wall {c.delta('wall_s'):+.0%}",
        ]
    lines += ["", "    skill_run_costs (the view obs/index.py ships):"]
    if view:
        for r in view:
            lines.append(
                f"      {r['skill']}  run {r['run_n']}  ${r['cost_usd'] or 0:.4f}  "
                f"{r['outcome']}"
            )
    else:
        lines.append("      (empty — no run recorded a skill use)")
    missing = [r.n for r in rows if r.run_n is None]
    if missing:
        lines.append(
            f"      absent from the view: run(s) {', '.join(map(str, missing))} "
            "— the view only sees a session that recorded a use"
        )
    if bad:
        lines += ["", "  runs whose number is not usable:"]
        lines += [f"    run {r.n}: {_answer_cell(r)}" for r in bad]
    lines.append("")
    return "\n".join(lines)


def write_board(path: Path, board: str, *, runs: int) -> None:
    """The generated half of the artifact. The commentary around it is written by
    hand, the same way `evals/demo/COMPARISON.md` is: a board can say what the
    numbers were, and only a human can say what they are worth."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Suite D — the skill cost curve\n\n"
        f"Produced {datetime.now():%Y-%m-%d} by `python -m evals.skills.run "
        f"--runs {runs}`.\n\n```\n" + board + "\n```\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.skills.run", description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="how many runs (default 5)")
    parser.add_argument(
        "--budget",
        type=float,
        default=0.03,
        help="per-run spend cap in USD (default 0.03)",
    )
    parser.add_argument("--max-steps", type=int, default=14)
    parser.add_argument("--write", type=Path, help="also write the board here")
    parser.add_argument(
        "--sessions", type=Path, help="where to write session dirs (default .sessions/)"
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        metavar="ROOT",
        help="re-derive the board from a finished run's session dirs, free",
    )
    args = parser.parse_args(argv)

    if args.rescore:
        rows, view = rescore(args.rescore)
        board = render(rows, view)
        print(board)
        if args.write:
            write_board(args.write, board, runs=len(rows))
            print(f"  board: {args.write}")
        print(f"  sessions: {args.rescore}\n")
        return 0 if all(r.ok for r in rows) else 1

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY not set — this suite runs a real model and costs "
            "money. Everything except the model call is covered offline by "
            "`make test` (see evals/test_skills.py)."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = args.sessions or Path(f".sessions/skills-{stamp}")
    skills_dir = root / "skills"
    index_db = root / "index.db"

    roots = []
    with CorpusServer() as server:
        for n in range(1, args.runs + 1):
            roots.append(
                run_once(
                    server,
                    root=root / f"run-{n}",
                    skills_dir=skills_dir,
                    index_db=index_db,
                    budget_usd=args.budget,
                    max_steps=args.max_steps,
                )
            )
        # The index refreshes at session *start*, so the last run is not in it
        # yet. Index once more here rather than leaving the final row missing.
        from pyharness.obs.index import update_index

        update_index(index_db, [root], skills_dir=skills_dir)
        rows = gather(roots, index_db, base=server.base)
        view = curve(index_db)

    board = render(rows, view)
    print(board)
    if args.write:
        write_board(args.write, board, runs=args.runs)
        print(f"  board: {args.write}")
    print(f"  sessions: {root}\n")
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
