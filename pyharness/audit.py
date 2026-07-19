from __future__ import annotations

import hashlib
import json
import threading
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
        # One chain serves the whole session tree, and spawned children run in
        # parent-side threads — the read-hash/append/advance sequence must be
        # atomic or concurrent writers fork the chain.
        self._lock = threading.Lock()

    def record(self, **fields: object) -> None:
        # Plain append (no fsync/rename): a crash mid-write can leave a torn
        # final line. That is tolerated, not prevented — `verify_chain` treats a
        # broken/torn final line as a broken chain (returns the bad index) rather
        # than raising, and `_last_hash`/`tail` skip an unparseable line, so a
        # reopened log continues cleanly from the last intact entry.
        with self._lock:
            entry = {"ts": time.time(), **fields}
            payload = json.dumps(entry, default=str, sort_keys=True)
            entry["prev"] = self._prev
            entry["hash"] = hashlib.sha256((self._prev + payload).encode()).hexdigest()
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            self._prev = entry["hash"]

    def tail(self, limit: int = 20, action: str | None = None) -> list[dict]:
        """The most recent audited calls (oldest first), so the agent can reflect
        on what it did — what it sent, where, whether it was allowed. The internal
        chain fields (`hash`/`prev`) are dropped, as are the `phase: "start"`
        intent records (each call writes two chained records; the agent reads
        outcomes, so only completed calls appear here); `action` filters by
        prefix (`"http"` for every HTTP call). Arguments are already the
        log-safe summary (secrets are referenced by name), so entries are safe
        to hand back."""
        if not self.path.exists():
            return []
        entries: list[dict] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry.pop("hash", None)
            entry.pop("prev", None)
            if entry.pop("phase", None) == "start":
                continue
            if action and not str(entry.get("action", "")).startswith(action):
                continue
            entries.append(entry)
        return entries[-limit:]


def _last_hash(path: Path) -> str:
    """The hash of the final intact entry, so a reopened log continues the same
    chain. A torn final line (a crash mid-append) is skipped so the next record
    chains off the last *parseable* entry rather than silently restarting the
    chain from genesis."""
    if not path.exists():
        return _GENESIS
    for line in reversed(path.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line).get("hash", _GENESIS)
        except json.JSONDecodeError:
            continue
    return _GENESIS


def verify_chain(path: str | Path) -> tuple[bool, int]:
    """Verify a log's hash chain. Returns ``(ok, bad_line)`` — ``bad_line`` is the
    0-based index of the first tampered/broken entry, or -1 when intact. An
    unparseable line — including a torn final line from a crash mid-append — is
    itself a broken chain: it returns ``(False, index)`` rather than raising."""
    path = Path(path)
    prev = _GENESIS
    index = -1
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        index += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, index
        recorded_hash = entry.pop("hash", None)
        recorded_prev = entry.pop("prev", None)
        payload = json.dumps(entry, default=str, sort_keys=True)
        expected = hashlib.sha256(((recorded_prev or "") + payload).encode()).hexdigest()
        if recorded_prev != prev or recorded_hash != expected:
            return False, index
        prev = recorded_hash
    return True, -1
