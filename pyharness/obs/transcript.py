"""Read-side views over one session's JSONL record.

A session dir holds the canonical record (`trace.jsonl`, `audit.jsonl`); this
module derives the compact views everything else shares: the flattened
transcript handed to LLM workers (`inspect_session`, the reflection pass), the
digest envelope the CLI's `run --json`/`show` print and the index stores, and
the one outcome vocabulary. Computed straight from the JSONL — never from the
index — so the views are correct even when the fail-open index update didn't
run.
"""

from __future__ import annotations

import json
from pathlib import Path

# Per-entry and total caps for the flattened transcript: enough that real
# sessions fit whole, bounded so a pathological trace can't blow a call.
_ENTRY_CAP = 4000
_TOTAL_CAP = 300_000

# The max_steps sentinel Agent.run returns as its answer when the step cap hits.
_MAX_STEPS_ANSWER = "(stopped: reached max_steps)"


def iter_jsonl(path: Path):
    """Yield parsed objects from a JSONL file, skipping blank/corrupt lines;
    a missing file yields nothing."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def render_transcript(trace_path: Path) -> str:
    """Flatten a trace.jsonl into a readable transcript. The per-call prompt
    snapshots (`system`/`messages` on llm_call entries) are dropped — they
    repeat the whole history every turn; the event stream itself IS the
    session."""
    if not trace_path.exists():
        raise FileNotFoundError(f"no trace at {trace_path}")
    parts: list[str] = []
    total = 0
    for e in iter_jsonl(trace_path):
        kind = e.get("kind")
        text = str(e.get("text") or "")
        if kind == "task":
            part = f"[user task]\n{text}"
        elif kind == "llm_call":
            part = f"[agent]\n{text}" if text else ""
        elif kind == "code":
            part = f"[code]\n{text}"
        elif kind == "output":
            part = f"[output]\n{text}"
        elif kind == "error":
            part = f"[error] {text}"
        elif kind == "llm_attempt":
            part = f"[llm attempt] {text}"
        elif kind == "skill_use":
            part = f"[skill_use] {e.get('skill')}: {e.get('outcome')} {e.get('note') or ''}".rstrip()
        elif kind == "answer":
            part = f"[final answer]\n{text}"
        elif kind == "session_end":
            part = f"[session end] spent ${e.get('spent_usd', 0):.4f} over {e.get('calls', 0)} LLM calls"
        else:  # session_start, note, budget — low-signal for inspection
            continue
        if not part:
            continue
        if len(part) > _ENTRY_CAP:
            part = part[:_ENTRY_CAP] + f"\n…[truncated, {len(part)} chars total]"
        parts.append(part)
        total += len(part)
        if total > _TOTAL_CAP:
            parts.append("…[transcript truncated]")
            break
    return "\n\n".join(parts)


def classify_outcome(
    *, answer: str | None, errors: int, tasks: int, stopped_budget: bool
) -> str:
    """The one outcome vocabulary, shared by the digest and the session index:
    answered | stopped:max_steps | stopped:budget | error | aborted | empty."""
    if answer == _MAX_STEPS_ANSWER:
        return "stopped:max_steps"
    if stopped_budget:
        return "stopped:budget"
    if answer is not None:
        return "answered"
    if errors:
        return "error"
    if tasks:
        return "aborted"  # a task ran but no answer or error landed (e.g. Ctrl-C)
    return "empty"


def session_digest(session_dir: str | Path) -> dict:
    """One compact envelope summarizing a session dir, from one scan of
    trace.jsonl + audit.jsonl. Every key is always present; `answer`/`task`
    are unclipped (the index clips at insert time)."""
    d = Path(session_dir).expanduser().resolve()
    trace_path = d / "trace.jsonl"
    audit_path = d / "audit.jsonl"

    started = ended = None
    task = answer = None
    tasks = steps = errors = failed_actions = llm_calls = 0
    cost = 0.0
    stopped_budget = False
    for e in iter_jsonl(trace_path):
        ts = e.get("ts")
        kind = e.get("kind")
        started = started if started is not None else ts
        ended = ts if ts is not None else ended
        if kind == "task":
            tasks += 1
            task = task or str(e.get("text") or "")
        elif kind == "llm_call":
            llm_calls += 1
            cost += e.get("cost_usd") or 0.0
        elif kind == "code":
            steps += 1
        elif kind == "error":
            errors += 1
            text = str(e.get("text") or "")
            stopped_budget = stopped_budget or text.startswith("BudgetExceeded")
        elif kind == "action_end":
            # Broker actions that raised (`ok: False`) — invisible to `errors`,
            # which counts orchestrator-level turn errors only.
            failed_actions += e.get("ok") is False
        elif kind == "answer":
            answer = str(e.get("text") or "")
        elif kind in ("budget", "session_end"):
            # spent_usd is cumulative for the session; prefer it over summed calls
            cost = max(cost, e.get("spent_usd") or 0.0)

    # The broker writes two chained audit records per action (an intent record
    # `phase: "start"` before execution, an outcome record `phase: "end"` after
    # — see Broker.call), so `actions` counts outcomes and an unpaired start is
    # an action killed too hard for even the teardown record: `aborted_actions`.
    # A pre-two-phase log has no `phase` fields; fall back to counting rows.
    actions = denials = starts = ends = 0
    for e in iter_jsonl(audit_path):
        actions += 1
        phase = e.get("phase")
        starts += phase == "start"
        ends += phase == "end"
        denials += e.get("decision") == "deny" or e.get("approved") is False
    aborted_actions = 0
    if starts or ends:
        actions = ends
        aborted_actions = max(0, starts - ends)

    return {
        "session": str(d),
        "name": d.name,
        "outcome": classify_outcome(
            answer=answer, errors=errors, tasks=tasks, stopped_budget=stopped_budget
        ),
        "answer": answer,
        "task": task,
        "tasks": tasks,
        "steps": steps,
        "llm_calls": llm_calls,
        "errors": errors,
        "failed_actions": failed_actions,
        "cost_usd": round(cost, 6),
        "actions": actions,
        "denials": denials,
        "aborted_actions": aborted_actions,
        "started": started,
        "ended": ended,
        "trace": str(trace_path),
        "audit": str(audit_path),
        "workspace": str(d / "workspace"),
    }


def latest_session(target: str | Path) -> Path | None:
    """The session dir to look at: `target` itself when it holds a trace.jsonl,
    else the most recently modified session dir under it, else None."""
    target = Path(target)
    if (target / "trace.jsonl").exists():
        return target
    candidates = [p for p in target.glob("*/trace.jsonl") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).parent
