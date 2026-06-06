from __future__ import annotations

import json
import time
from pathlib import Path


class AuditLog:
    """Append-only JSONL record of every capability call.

    This is both the safety record and the primary debugging trail. Secrets are
    never arguments to capabilities (they are referenced by name and injected in
    the parent), so logged arguments are safe to persist.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields: object) -> None:
        entry = {"ts": time.time(), **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
