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

from pathlib import Path

from ...index import SCHEMA_HELP, query
from ...transcript import render_transcript

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
        transcript = render_transcript(Path(rows[0]["id"]) / "trace.jsonl")
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
