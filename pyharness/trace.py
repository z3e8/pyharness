from __future__ import annotations

import json
import time
from pathlib import Path


class TraceLog:
    """Append-only JSONL record of the agent's turn-by-turn work — the user task,
    the agent's notes, each code cell and its output, the final answer, and a
    budget snapshot.

    The audit log (see audit.py) records *capability calls*; this records the
    *orchestration* around them. Together they are the durable source the
    observability UI reads (the live CLI trace is the same stream, unsaved)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, text: str = "", **extra: object) -> None:
        entry = {"ts": time.time(), "kind": kind, "text": text, **extra}
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
