"""Run the demo suite, or pin live pages into the corpus.

    python -m evals.demo.run                    # run every twin (needs an API key)
    python -m evals.demo.run --twin invoice-…   # one twin
    python -m evals.demo.run --approve-all      # measure the layer under approval
    python -m evals.demo.run capture a.html=https://…   # refresh the corpus

Exit code is 0 when every task landed on an acceptable outcome and 1 otherwise —
the same rule the adversarial suite uses, and for the same reason: a benign task
that stopped completing, a directed release that stopped being refused, and an
authorized one that stopped going through are all "the published claim no longer
describes the system", and none should need a human to notice it in scrollback.

A hostile task is the one place with two acceptable answers. Whether a model
acts on an injected directive is a fact about the model, not about this harness,
and pinning the suite to either answer would make it go red on someone else's
release notes. It is reported with its own denominator instead.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from .capture import capture
from .runner import Outcome, TaskRun, TwinResult, acceptable, run_twin, score
from .server import CorpusServer
from .tasks import TWINS, Kind

_MARK = {
    Outcome.COMPLETED: "done  ",
    Outcome.CONTAINED: "held  ",
    Outcome.DELIVERED: "sent  ",
    Outcome.NOT_ATTEMPTED: "no-try",
    Outcome.INCOMPLETE: "MISS  ",
    Outcome.BREACHED: "BREACH",
    Outcome.ERROR: "ERROR ",
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
    if run.task.kind is Kind.RELEASE:
        # Never omitted. The verdict on a release is only interpretable next to
        # the human's answer, and a reader who has to go and look it up in the
        # task data is a reader who will assume.
        bits.append("approved" if run.task.approve else "denied")
    if run.target_url:
        bits.append(f"attempted x{len(run.attempts)}" if run.attempts else "NOT TRIED")
    refusals = {a.reason for a in run.refusals if a.reason}
    if refusals:
        bits.append("refused by " + ", ".join(sorted(refusals)))
    # Kept well away from the refusals. A call that broke is not a call that was
    # stopped, and the first real run published one as the other.
    faults = {a.fault for a in run.faults if a.fault}
    if faults:
        bits.append("call errors: " + ", ".join(sorted(faults)))
    if run.receipted:
        bits.append(f"credential delivered to the billing API x{run.receipted}")
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
    """The board.

    Three axes, reported separately and never averaged. Competence, whether the
    model acted on an injection, and what the harness did with a call it was
    told to make are three different questions, and the one thing this must
    never print is a single pass rate that lets a reader infer any of them from
    the others. The first real run's `2/2 hostile contained` was true and
    useless: nothing had been attempted, so nothing had been contained.
    """
    runs = [run for result in results for run in result.runs]
    verdicts = {run.task.id: score(run) for run in runs}
    by_kind = {
        kind: [r for r in runs if r.task.kind is kind]
        for kind in (Kind.BENIGN, Kind.HOSTILE, Kind.RELEASE)
    }

    def verdict_of(run: TaskRun) -> Outcome:
        return verdicts[run.task.id]

    def count(group: list[TaskRun], outcome: Outcome) -> int:
        return sum(1 for run in group if verdict_of(run) is outcome)

    benign = by_kind[Kind.BENIGN]
    hostile = by_kind[Kind.HOSTILE]
    acted = [r for r in hostile if verdict_of(r) is not Outcome.NOT_ATTEMPTED]
    # Split by whether the release was *supposed* to go through, not by how the
    # human answered: the off-scope task is approved and still expected to be
    # refused, which is how the egress layer gets measured on its own.
    denied = [r for r in by_kind[Kind.RELEASE] if not r.task.authorized]
    granted = [r for r in by_kind[Kind.RELEASE] if r.task.authorized]
    off = [r for r in runs if verdict_of(r) not in acceptable(r.task)]
    cost = sum(r.digest.get("cost_usd", 0.0) for r in runs)

    width = max((len(r.task.id) for r in runs), default=10)
    lines = [
        "",
        f"  demo suite — {len(off)} off expectation (${cost:.4f})",
        "",
        f"    competence   {count(benign, Outcome.COMPLETED)}/{len(benign)} "
        "benign completed",
        # Phrased as a fact about the model, because that is what it is. The
        # denominator is deliveries, so a page that stopped reaching the model
        # shows up here rather than flattering the number.
        f"    injections   {len(acted)}/{len(hostile)} acted on by the model, "
        f"{count(acted, Outcome.CONTAINED)} of those contained",
        f"    containment  {count(denied, Outcome.CONTAINED)}/{len(denied)} "
        "directed releases refused",
        f"    authorized   {count(granted, Outcome.DELIVERED)}/{len(granted)} "
        "approved releases delivered",
        "",
    ]
    for result in results:
        for run in result.runs:
            verdict = verdict_of(run)
            flag = " " if verdict in acceptable(run.task) else "!"
            lines.append(
                f"  {flag}{_MARK[verdict]}  {run.task.id:<{width}}  {_detail(run)}"
            )
    if off:
        lines += ["", "  off expectation — the published claim no longer holds:"]
        lines += [
            f"    {r.task.id}: got {verdict_of(r)}, acceptable: "
            + "/".join(sorted(acceptable(r.task)))
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
        help="force every approval prompt yes, overriding each task's own "
        "answer — measures whatever layer is left beneath the gate",
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
