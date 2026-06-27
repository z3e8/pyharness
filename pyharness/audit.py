from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

_GENESIS = ""


class AuditLog:
    """Append-only, tamper-evident JSONL record of every capability call.

    This is both the safety record and the primary debugging trail. Secrets are
    never arguments to capabilities (they are referenced by name and injected in
    the parent), so logged arguments are safe to persist.

    Each entry carries a hash chain: ``hash = sha256(prev_hash + entry)`` and
    ``prev`` points at the previous entry's hash. Any edit, deletion, or
    reordering after the fact breaks the chain, so the record is verifiable
    (see :func:`verify_chain`) — important once this log is shipped off-box.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev = _last_hash(self.path)

    def record(self, **fields: object) -> None:
        entry = {"ts": time.time(), **fields}
        payload = json.dumps(entry, default=str, sort_keys=True)
        entry["prev"] = self._prev
        entry["hash"] = hashlib.sha256((self._prev + payload).encode()).hexdigest()
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self._prev = entry["hash"]


def _last_hash(path: Path) -> str:
    """The hash of the final entry, so a reopened log continues the same chain."""
    if not path.exists():
        return _GENESIS
    last = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            last = line
    if not last:
        return _GENESIS
    try:
        return json.loads(last).get("hash", _GENESIS)
    except json.JSONDecodeError:
        return _GENESIS


def verify_chain(path: str | Path) -> tuple[bool, int]:
    """Verify a log's hash chain. Returns ``(ok, bad_line)`` — ``bad_line`` is the
    0-based index of the first tampered/broken entry, or -1 when intact."""
    path = Path(path)
    prev = _GENESIS
    index = -1
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        index += 1
        entry = json.loads(line)
        recorded_hash = entry.pop("hash", None)
        recorded_prev = entry.pop("prev", None)
        payload = json.dumps(entry, default=str, sort_keys=True)
        expected = hashlib.sha256(((recorded_prev or "") + payload).encode()).hexdigest()
        if recorded_prev != prev or recorded_hash != expected:
            return False, index
        prev = recorded_hash
    return True, -1
