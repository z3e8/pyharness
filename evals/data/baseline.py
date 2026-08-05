"""The control arms: the same task with a conventional tool loop instead.

Two of them, because there are two different claims to separate and one arm
cannot separate them.

**`files`** — `list_files` and `read_file`, the tool pair almost every agent
framework ships. `read_file` transparently decompresses `.gz` and returns a
*line range*, which is more than the usual implementation gives you. That
generosity is the point: the arm has to fail on volume, not on encoding, or the
result is about gzip rather than about the action space, and a reader would be
right to say the corpus was rigged.

**`shell`** — one tool, running commands in the data directory. It can `zcat`,
`awk`, and `python -c`, so it can genuinely win this task. That is not a flaw in
the control, it is the honest finding: *code* is the capability, and a shell is
one way to get it. What a shell does not get you is a persistent kernel — every
call is a fresh process, so state does not survive between steps and each
exploratory pass re-reads the corpus from disk.

Neither arm is a straw man. Same model, same tier, same prompt, same budget, and
tools that work correctly and are documented clearly. This is what a competent
engineer wires up in an afternoon, which is the only comparison worth drawing.

**One concession, documented.** The `shell` arm runs model-authored commands
with no sandbox — that is what "no broker" means, and sandboxing it would be
measuring this harness's sandbox instead of the absence of one. A small denylist
of unmistakably destructive patterns is enforced anyway, so an eval run cannot
wreck the machine it is running on. Nothing the task needs is on that list, so it
does not touch the measurement; it is a seatbelt, not a policy.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
from pathlib import Path

from .gen import Truth
from .runner import ArmRun
from .tasks import BUDGET_USD, MAX_TURNS, judge, prompt

# Per tool result. Matches the demo arm's cap. Generous enough to read a real
# slice of a file, small enough that the arm cannot launder the corpus through
# its context a megabyte at a time.
_MAX_OUTPUT = 8000

_SYSTEM = (
    "You are an autonomous data analyst. Use the tools to answer the user's "
    "question, then reply with the answer."
)

_FILE_TOOLS = [
    {
        "name": "list_files",
        "description": "List the files in the data directory with their sizes in bytes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_file",
        "description": (
            "Read a range of lines from a file in the data directory. Files ending "
            "in .gz are decompressed transparently. Returns at most 200 lines and "
            "is truncated to 8000 characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "0-based, default 0"},
                "max_lines": {"type": "integer", "description": "default 200, max 200"},
            },
            "required": ["path"],
        },
    },
]

_SHELL_TOOLS = [
    {
        "name": "shell",
        "description": (
            "Run a shell command in the data directory and return its combined "
            "stdout and stderr, truncated to 8000 characters. Each call runs in a "
            "fresh process; nothing persists between calls except files on disk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

ARMS = {"files": _FILE_TOOLS, "shell": _SHELL_TOOLS}

# Patterns that could damage the machine running the eval and that no correct
# solution to this task has any reason to use. Refused with an explanation, so
# the model can adapt rather than silently losing a turn.
_FORBIDDEN = re.compile(
    r"(\brm\s+-[rf]|\bmkfs\b|\bdd\s+if=|>\s*/dev/|\bshutdown\b|\breboot\b|"
    r"\bcurl\b|\bwget\b|\bnc\b|\bssh\b|:\(\)\s*\{)",
)


def _list_files(data_dir: Path) -> str:
    rows = [
        f"{path.name}  {path.stat().st_size} bytes"
        for path in sorted(data_dir.rglob("*"))
        if path.is_file()
    ]
    return "\n".join(rows) or "(empty)"


def _read_file(data_dir: Path, args: dict) -> str:
    target = (data_dir / str(args.get("path", ""))).resolve()
    if not target.is_relative_to(data_dir.resolve()) or not target.is_file():
        return f"error: no such file {args.get('path')!r}"
    start = max(0, int(args.get("start_line") or 0))
    limit = min(200, max(1, int(args.get("max_lines") or 200)))
    opener = gzip.open if target.suffix == ".gz" else open
    lines = []
    with opener(target, "rt", errors="replace") as handle:  # type: ignore[operator]
        for index, line in enumerate(handle):
            if index < start:
                continue
            if len(lines) >= limit:
                break
            lines.append(line.rstrip("\n"))
    return "\n".join(lines) or "(no lines in range)"


def _shell(data_dir: Path, args: dict) -> str:
    command = str(args.get("command", ""))
    if _FORBIDDEN.search(command):
        return (
            "error: refused. This sandboxless runner blocks destructive and "
            "network commands. Nothing this task needs is blocked — use the "
            "local files."
        )
    try:
        done = subprocess.run(
            command,
            shell=True,
            cwd=data_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "error: command timed out after 120s"
    except Exception as exc:  # noqa: BLE001 — a naive loop reports and continues
        return f"error: {type(exc).__name__}: {exc}"
    return (done.stdout + done.stderr).strip() or f"(no output, exit {done.returncode})"


def _call_tool(arm: str, name: str, args: dict, data_dir: Path) -> str:
    try:
        if name == "list_files":
            return _list_files(data_dir)
        if name == "read_file":
            return _read_file(data_dir, args)
        if name == "shell":
            return _shell(data_dir, args)
    except Exception as exc:  # noqa: BLE001 — a naive loop reports and continues
        return f"error: {type(exc).__name__}: {exc}"
    return f"error: no such tool {name!r}"


def run_baseline(
    arm: str,
    data_dir: Path,
    truth: Truth,
    *,
    client=None,
    tier: str = "mid",
) -> ArmRun:
    """Run the task through one control arm.

    `client=None` builds a real client and costs money; the tests pass a
    scripted double.
    """
    from pyharness.budget import Budget

    budget = Budget(limit_usd=BUDGET_USD)
    if client is None:
        from pyharness.llm.client import AnthropicLLM

        client = AnthropicLLM(budget=budget)

    run = ArmRun(arm=arm)
    tools = ARMS[arm]
    messages: list[dict] = [{"role": "user", "content": prompt()}]
    try:
        for _ in range(MAX_TURNS):
            budget.check()
            reply = client.complete(
                system=_SYSTEM, messages=messages, tier=tier, tools=tools
            )
            messages.append({"role": "assistant", "content": reply.content})
            if not reply.tool_calls:
                run.answer = reply.text.strip()
                break
            run.steps += len(reply.tool_calls)
            results = []
            for call in reply.tool_calls:
                output = _call_tool(arm, call.name, dict(call.input), data_dir)
                if output.startswith("error:"):
                    run.errors += 1
                run.transcript.append(
                    {
                        "turn": len(run.transcript) + 1,
                        "thinking": reply.text.strip(),
                        "tool": call.name,
                        "input": dict(call.input),
                        # Clipped for the artifact, not for the model — the model
                        # saw `_MAX_OUTPUT`. A transcript that reprinted 8KB of
                        # log lines per turn would be unreadable, which defeats
                        # the point of keeping it.
                        "output": output[:1200],
                        "output_chars": len(output),
                    }
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": output[:_MAX_OUTPUT],
                    }
                )
            messages.append({"role": "user", "content": results})
        else:
            run.notes.append(f"hit the {MAX_TURNS}-turn ceiling without answering")
    except Exception as exc:  # noqa: BLE001 — recorded, then scored
        run.broke = f"{type(exc).__name__}: {exc}"
    run.cost_usd = budget.spent_usd
    run.verdict = judge(run.answer, truth)
    return run


_ARM_BLURB = {
    "files": (
        "`list_files` and `read_file` — the tool pair almost every agent "
        "framework ships. This `read_file` decompresses `.gz` and takes a line "
        "range, which is more than most implementations give you: the arm has "
        "to fail on volume, not on encoding."
    ),
    "shell": (
        "One `shell` tool, running in the data directory. It can `awk` and "
        "`python -c`, so it can genuinely win this task — that is the honest "
        "finding rather than a flaw in the control. What it does not get is a "
        "persistent kernel: every call is a fresh process."
    ),
}


def render_transcript(run: ArmRun, truth: Truth) -> str:
    """One control arm's run, as a page a reader can scroll.

    Written as markdown so the existing static renderer bakes it beside the
    brokered arm's session (`pyharness-watch --static --doc`), which is what
    makes the three arms comparable by *reading* rather than by trusting three
    numbers on a board.
    """
    verdict = run.verdict
    lines = [
        f"# Control arm: `{run.arm}`",
        "",
        _ARM_BLURB.get(run.arm, ""),
        "",
        f"**Score {run.score}/3** · ${run.cost_usd:.4f} · {run.steps} tool calls"
        + (f" · {run.errors} tool errors" if run.errors else ""),
        "",
        f"The corpus is {truth.bytes_uncompressed / 1e6:.0f}MB uncompressed across "
        f"30 files — more than this arm can read into its context. The answer is "
        f"`requests={truth.requests}`, `breach_day={truth.breach_day}`, "
        f"`peak_hour={truth.peak_hour}`; the shortcut that trusts the data yields "
        f"`{truth.naive_requests}`, `{truth.naive_breach_day}`, "
        f"`{truth.naive_peak_hour}`.",
        "",
    ]
    if verdict:
        for name in ("requests", "breach_day", "peak_hour"):
            got = verdict.answers.get(name)
            mark = (
                "correct"
                if name in verdict.correct
                else "**the naive answer**"
                if name in verdict.naive
                else "not answered"
                if name in verdict.missing
                else "wrong"
            )
            lines.append(f"- `{name}` → `{got}` — {mark}")
        lines.append("")
    for note in run.notes:
        lines.append(f"> {note}\n")
    if run.broke:
        lines.append(f"> Run ended on `{run.broke}`\n")

    lines.append("## Turns\n")
    for entry in run.transcript:
        lines.append(f"### {entry['turn']}. `{entry['tool']}`\n")
        if entry.get("thinking"):
            lines.append(f"{entry['thinking']}\n")
        lines.append(f"```json\n{json.dumps(entry['input'], indent=2)}\n```\n")
        clipped = entry["output_chars"] > len(entry["output"])
        lines.append(
            f"```\n{entry['output']}"
            + (f"\n… [{entry['output_chars']:,} chars total]" if clipped else "")
            + "\n```\n"
        )
    if not run.transcript:
        lines.append("_No tool calls were made._\n")

    lines.append("## Final answer\n")
    lines.append(f"```\n{run.answer or '(none)'}\n```\n")
    return "\n".join(lines)
