"""Run the throughput suite and write its board.

    python -m evals.data.run                     # all three arms (needs an API key)
    python -m evals.data.run --arm brokered      # one arm
    python -m evals.data.run --rows 2000         # a smaller corpus
    python -m evals.data.run --write evals/data/BOARD.md

**What the exit code gates on, and what it deliberately does not.** It fails if
the corpus stops discriminating — `gen.verify` asserts that every naive shortcut
still yields a different answer from the correct one, and a corpus that has lost
that property is measuring nothing while still printing green. It also fails if
the brokered arm cannot produce an answer at all, which is the harness being
broken.

It does *not* fail on the brokered arm getting a question wrong. How many of the
three a model gets right is a fact about the model, and pinning a commit gate to
it would turn someone else's release notes into a red build here. The score is
published with its denominator instead — the same rule the demo suite uses for
whether a model acts on an injected directive, and for the same reason.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .baseline import ARMS, render_transcript, run_baseline
from .gen import DEFECTS, SLO_MS, Truth, verify
from .runner import ArmRun, give_corpus, run_brokered, save_truth, stage_corpus

ARM_NAMES = ("brokered", *ARMS)


def _questions(run: ArmRun) -> str:
    """Per-question marks: what was answered, and when wrong, wrong *how*."""
    verdict = run.verdict
    if verdict is None:
        return "no answer"
    marks = []
    for name in ("requests", "breach_day", "peak_hour"):
        if name in verdict.correct:
            marks.append(f"{name}=ok")
        elif name in verdict.naive:
            marks.append(f"{name}=NAIVE")
        elif name in verdict.missing:
            marks.append(f"{name}=-")
        else:
            marks.append(f"{name}=wrong")
    return "  ".join(marks)


def render(runs: list[ArmRun], truth: Truth) -> str:
    """The board. States the answer key first, because a reader who cannot see
    the wrong answer that was available cannot size the right one."""
    lines = [
        "",
        f"  throughput suite — {truth.requests:,} requests across 30 files, "
        f"{truth.bytes_uncompressed / 1e6:.0f}MB uncompressed",
        "",
        "    answer key    "
        f"requests={truth.requests}  breach_day={truth.breach_day}  "
        f"peak_hour={truth.peak_hour}",
        "    the shortcut  "
        f"requests={truth.naive_requests}  breach_day={truth.naive_breach_day}  "
        f"peak_hour={truth.naive_peak_hour}",
        "",
    ]
    width = max(len(run.arm) for run in runs)
    for run in runs:
        bits = [
            f"{run.score}/3",
            f"${run.cost_usd:.4f}",
            f"{run.steps} steps",
        ]
        if run.errors:
            bits.append(f"{run.errors} tool errors")
        if run.verdict:
            handled = run.verdict.defects_neutralised(truth)
            bits.append(
                f"defects handled: {', '.join(handled)}"
                if handled
                else "no defects handled"
            )
        for note in run.notes:
            bits.append(note)
        if run.broke:
            bits.append(run.broke)
        lines.append(f"  {run.arm:<{width}}  {'; '.join(bits)}")
        lines.append(f"  {'':<{width}}  {_questions(run)}")
        lines.append("")
    return "\n".join(lines)


def write_board(path: Path, board: str, truth: Truth, *, model: str) -> None:
    """The committed artifact. Dated, because refreshing it costs a model call."""
    defect_rows = "\n".join(f"| `{slug}` | {what} |" for slug, what in DEFECTS.items())
    path.write_text(
        "# Throughput suite — brokered vs two control arms\n\n"
        f"**Produced {datetime.now():%Y-%m-%d}** by `python -m evals.data.run` "
        f"against {model}, on a corpus of {truth.requests:,} requests across 30 "
        f"gzipped files ({truth.bytes_uncompressed / 1e6:.0f}MB uncompressed — "
        "roughly ten times a large context window). Refreshing it costs a real\n"
        "model call in each of three arms, so it is committed as a dated artifact.\n"
        "The corpus itself is not committed: it is regenerated from a seed by\n"
        "`evals/data/gen.py`, and the answer key is derived by reading the written\n"
        "bytes back rather than from the generator's intent.\n\n"
        "## The task\n\n"
        "Three questions about a month of service logs: total requests, the day\n"
        f"`/checkout` p95 first breached {SLO_MS}ms, and the busiest hour in UTC.\n"
        "Every arm gets the same prompt, the same model, the same tier and the\n"
        "same budget. They differ only in what they can do.\n\n"
        "## What is wrong with the data\n\n"
        "Five defects, each load-bearing for exactly one question. None is named\n"
        "in the prompt.\n\n"
        "| Defect | What it is |\n|---|---|\n" + defect_rows + "\n\n"
        "A shortcut that ignores them is *available* and produces a specific set\n"
        "of plausible wrong numbers — that is what makes a correct answer\n"
        "evidence rather than arithmetic. Which defects an arm handled is derived\n"
        "from the integers it returned, never from a keyword scan of its prose.\n\n"
        "## The arms\n\n"
        "- **brokered** — this harness. Python action space, persistent kernel.\n"
        "- **files** — `list_files` + `read_file`, the conventional pair. Its\n"
        "  `read_file` decompresses `.gz` and takes a line range, so the arm\n"
        "  fails on volume rather than on encoding.\n"
        "- **shell** — one `shell` tool in the data directory. It can `awk` and\n"
        "  `python -c`, so it can genuinely win. It has no persistent kernel:\n"
        "  every call is a fresh process.\n\n"
        "## Board\n```\n" + board + "\n```\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.data.run")
    parser.add_argument("--arm", choices=ARM_NAMES, action="append")
    parser.add_argument("--rows", type=int, default=10_000, help="rows per day")
    parser.add_argument("--seed", type=int, default=20260601)
    parser.add_argument("--tier", default="mid", choices=["cheap", "mid", "smart"])
    parser.add_argument("--write", type=Path, help="write the board here")
    parser.add_argument(
        "--keep", action="store_true", help="keep the generated corpus on disk"
    )
    args = parser.parse_args(argv)
    arms = args.arm or list(ARM_NAMES)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(".sessions") / f"data-eval-{stamp}"
    truth = stage_corpus(root, rows_per_day=args.rows, seed=args.seed)
    try:
        verify(truth)
    except AssertionError as exc:
        print(f"corpus no longer discriminates: {exc}", file=sys.stderr)
        return 1

    runs: list[ArmRun] = []
    for arm in arms:
        print(f"running {arm}…", file=sys.stderr)
        if arm == "brokered":
            # The brokered arm's data has to land inside its own session
            # workspace; every other path the agent could use is refused.
            session_root = root / "brokered"
            give_corpus(root, session_root / "workspace" / "logs")
            runs.append(run_brokered(session_root, truth, tier=args.tier))
        else:
            data_dir = give_corpus(root, root / f"arm-{arm}" / "logs")
            run = run_baseline(arm, data_dir, truth, tier=args.tier)
            # The control arms are not sessions, so nothing else would record
            # them. Written next to the brokered arm's trace so all three are
            # inspectable after the run rather than only while it is alive.
            (root / f"arm-{arm}" / "transcript.md").write_text(
                render_transcript(run, truth)
            )
            (root / f"arm-{arm}" / "transcript.json").write_text(
                json.dumps(run.transcript, indent=2)
            )
            runs.append(run)

    # Only now. While an arm was running, the key existed nowhere on disk.
    save_truth(root, truth)

    board = render(runs, truth)
    print(board)
    if args.write:
        write_board(args.write, board, truth, model=args.tier)
        print(f"wrote {args.write}", file=sys.stderr)
    if not args.keep:
        # ~7MB per arm plus the staging copy. The session's trace, audit chain
        # and truth key stay — those are the record; the corpus regenerates.
        shutil.rmtree(root / "corpus", ignore_errors=True)
        for arm in arms:
            where = (
                root / "brokered" / "workspace" / "logs"
                if arm == "brokered"
                else root / f"arm-{arm}" / "logs"
            )
            shutil.rmtree(where, ignore_errors=True)

    # Say where the evidence is and how to turn it into one page. A run whose
    # artifacts the operator has to go and find is a run that gets rerun.
    docs = " ".join(
        f'--doc "Control arm: {arm}={root}/arm-{arm}/transcript.md"'
        for arm in arms
        if arm != "brokered"
    )
    print(f"\nartifacts under {root}/", file=sys.stderr)
    if "brokered" in arms:
        print(
            "bake all arms into one self-contained page:\n"
            f"  uv run pyharness-watch {root}/brokered --static {root}/site \\\n"
            f'    --title "Throughput suite" '
            f'--doc "Board={args.write or "evals/data/BOARD.md"}" {docs}',
            file=sys.stderr,
        )

    brokered = next((r for r in runs if r.arm == "brokered"), None)
    if brokered and brokered.verdict and len(brokered.verdict.missing) == 3:
        print("brokered arm produced no answer at all", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
