"""Observability builtins: the agent's window onto its own past.

Two complementary reads over the session record (design: direction 5):
`stats` answers aggregate questions from the derived SQLite index (success
rates, cost trends, error taxonomy) without rereading transcripts;
`inspect_session` answers a targeted question about one past session by
handing its full transcript to a cheap-model worker — the orchestrator pays
a paragraph, not the trace.

Both are reads: the index connection is read-only (ATTACH denied), and the
worker only summarizes. The current session is not in the index yet — these
look at the past.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...index import SCHEMA_HELP, query

# Per-entry and total caps for the transcript handed to the worker: enough that
# real sessions fit whole, bounded so a pathological trace can't blow the call.
_ENTRY_CAP = 4000
_TOTAL_CAP = 300_000

_INSPECT_SYSTEM = """\
You answer questions about one past session of an autonomous Python agent.
Below is that session's transcript: the user task(s), the agent's messages,
the code it ran, outputs, errors, skill uses, and the final answer. Answer the
question factually from the transcript alone; quote the decisive lines (a
selector, an error, a URL) when they matter. If the transcript does not
contain the answer, say so plainly. Be concise: a short paragraph, or a few
bullets when listing."""


class ObservabilityCapability:
    """`stats` (read-only SQL over the session index) and `inspect_session`
    (delegated inspection of one past transcript)."""

    name = "obs"

    def __init__(self, index_db: str | Path | None, llm):
        self.index_db = Path(index_db).expanduser() if index_db else None
        self.llm = llm

    def exports(self) -> dict:
        return {"stats": self.stats, "inspect_session": self.inspect_session}

    def _require_db(self) -> Path:
        if self.index_db is None:
            raise RuntimeError(
                "no session index configured for this session (index_db unset)"
            )
        return self.index_db

    def stats(self, sql: str | None = None, limit: int = 200):
        """Query your session index — every past session, LLM call, capability
        call, error, and skill use, already aggregated. Read-only SQL, rows as
        dicts (capped at `limit`). Call with no arguments to get the schema.
        Examples:
            stats("SELECT * FROM skill_stats")
            stats("SELECT name, task, outcome, cost_usd FROM sessions ORDER BY started DESC LIMIT 10")
            stats("SELECT * FROM error_taxonomy LIMIT 5")"""
        if sql is None:
            return SCHEMA_HELP
        return query(self._require_db(), sql, limit=limit)

    def inspect_session(self, session: str, question: str) -> str:
        """Ask a question about one past session — "why did the greenhouse skill
        fail?", "what selector did I use for the login form?". A cheap worker
        reads that session's full transcript and returns the answer, so you don't
        have to. `session` is a name or id from stats() (e.g.
        "cli-20260712-104734")."""
        rows = query(
            self._require_db(),
            "SELECT id FROM sessions WHERE name = ? OR id = ? LIMIT 1",
            params=(session, session),
        )
        if not rows:
            raise KeyError(f"no session {session!r} in the index — see stats()")
        transcript = _render_transcript(Path(rows[0]["id"]) / "trace.jsonl")
        completion = self.llm.complete(
            system=_INSPECT_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"{transcript}\n\n---\nQuestion: {question}",
                }
            ],
            tier="cheap",
        )
        return completion.text


def _render_transcript(trace_path: Path) -> str:
    """Flatten a trace.jsonl into a readable transcript for the worker. The
    per-call prompt snapshots (`system`/`messages` on llm_call entries) are
    dropped — they repeat the whole history every turn; the event stream itself
    IS the session."""
    if not trace_path.exists():
        raise FileNotFoundError(f"no trace at {trace_path}")
    parts: list[str] = []
    total = 0
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
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
