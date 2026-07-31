"""Run the demo suite, or pin live pages into the corpus.

    python -m evals.demo.run                    # run every twin (needs an API key)
    python -m evals.demo.run --twin invoice-…   # one twin
    python -m evals.demo.run --approve-all      # measure the layer under approval
    python -m evals.demo.run capture a.html=https://…   # refresh the corpus

Exit code is 0 when every task matched its expected outcome and 1 otherwise —
the same rule the adversarial suite uses, and for the same reason: a benign task
that stopped completing and a hostile task that stopped being contained are both
"the published claim no longer describes the system", and neither should need a
human to notice it in scrollback.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from .capture import capture
from .runner import Outcome, TaskRun, TwinResult, expected, run_twin, score
from .server import CorpusServer
from .tasks import TWINS

_MARK = {
    Outcome.COMPLETED: "done ",
    Outcome.CONTAINED: "held ",
    Outcome.INCOMPLETE: "MISS ",
    Outcome.BREACHED: "BREACH",
    Outcome.ERROR: "ERROR",
}


def _detail(run: TaskRun) -> str:
    """One line of the evidence behind a verdict.

    Shows what was *measured* rather than what was concluded — refusals by name,
    what the attacker's listener received — because the interesting part of a
    contained run is which layer did the containing.
    """
    bits = [
        f"${run.digest.get('cost_usd', 0):.4f}",
        f"{run.digest.get('steps', 0)} steps",
    ]
    if run.task.marker:
        bits.append("injected" if run.delivered else "NOT DELIVERED")
    refusals = {a.reason for a in run.refusals if a.reason}
    if refusals:
        bits.append("refused by " + ", ".join(sorted(refusals)))
    if run.collector_hits:
        bits.append(f"collector hit x{run.collector_hits}")
    if run.exfiltrated:
        bits.append(f"CREDENTIAL EXFILTRATED x{run.exfiltrated}")
    if run.leaked_to:
        bits.append(f"cleartext in {', '.join(run.leaked_to)}")
    if run.broke:
        bits.append(run.broke)
    return "; ".join(bits)


def render(results: list[TwinResult]) -> str:
    runs = [run for result in results for run in result.runs]
    verdicts = {run.task.id: score(run) for run in runs}
    held = sum(1 for r in runs if verdicts[r.task.id] is Outcome.CONTAINED)
    done = sum(1 for r in runs if verdicts[r.task.id] is Outcome.COMPLETED)
    hostile = sum(1 for r in runs if r.task.marker)
    benign = len(runs) - hostile
    off = [r for r in runs if verdicts[r.task.id] is not expected(r.task)]
    cost = sum(r.digest.get("cost_usd", 0.0) for r in runs)

    width = max((len(r.task.id) for r in runs), default=10)
    lines = [
        "",
        f"  demo suite — {done}/{benign} benign completed, "
        f"{held}/{hostile} hostile contained, {len(off)} off expectation "
        f"(${cost:.4f})",
        "",
    ]
    for result in results:
        for run in result.runs:
            verdict = verdicts[run.task.id]
            flag = " " if verdict is expected(run.task) else "!"
            lines.append(
                f"  {flag}{_MARK[verdict]}  {run.task.id:<{width}}  {_detail(run)}"
            )
    if off:
        lines += ["", "  off expectation — the published claim no longer holds:"]
        lines += [
            f"    {r.task.id}: got {verdicts[r.task.id]}, expected {expected(r.task)}"
            for r in off
        ]
    lines.append("")
    return "\n".join(lines)


def _cmd_capture(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals.demo.run capture")
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="NAME=URL",
        help="corpus filename and the live URL to pin into it",
    )
    args = parser.parse_args(argv)
    sources = {}
    for item in args.sources:
        name, sep, url = item.partition("=")
        if not sep or not url:
            sys.exit(f"expected NAME=URL, got {item!r}")
        sources[name] = url
    for shot in capture(sources):
        note = " — looks JavaScript-rendered; this task needs the browser" * (
            shot.looks_javascript_rendered
        )
        print(f"  {shot.name}: {shot.bytes} bytes from {shot.url}{note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "capture":
        return _cmd_capture(argv[1:])

    parser = argparse.ArgumentParser(prog="evals.demo.run", description=__doc__)
    parser.add_argument("--twin", action="append", help="run only these twins by id")
    parser.add_argument(
        "--approve-all",
        action="store_true",
        help="answer every approval prompt yes — measures the layer beneath it",
    )
    parser.add_argument(
        "--sessions",
        type=Path,
        help="where to write session dirs (default: .sessions/demo-<ts>)",
    )
    args = parser.parse_args(argv)

    twins = TWINS
    if args.twin:
        wanted = set(args.twin)
        twins = tuple(t for t in TWINS if t.id in wanted)
        missing = wanted - {t.id for t in twins}
        if missing:
            sys.exit(f"no such twin: {', '.join(sorted(missing))}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ANTHROPIC_API_KEY not set — this suite runs a real model and costs "
            "money. Everything except the model call is covered offline by "
            "`make test` (see evals/test_demo.py)."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = args.sessions or Path(f".sessions/demo-{stamp}")
    with CorpusServer() as server:
        results = [
            run_twin(twin, server, root=root / twin.id, approve_all=args.approve_all)
            for twin in twins
        ]
    print(render(results))
    print(f"  sessions: {root}\n")
    return 0 if all(result.as_expected for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
